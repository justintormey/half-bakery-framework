#!/usr/bin/env python3
"""Half Bakery v2 Dispatcher — polls GitHub Projects and spawns Claude agents.

Runs every 5 minutes via launchd. Deterministic dispatch, stateless agents.
All coordination stays in this script; agents get only their persona + assignment.
"""

import argparse
import fcntl
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Smart dispatcher v3 modules
from budget import get_budget_profile, should_defer_issue, update_session_stats, get_budget_summary
from discoverer import phase_discover
from evaluator import evaluate, classify_issue, _extract_summary as eval_extract_summary, extract_verdict
from usage_tracker import get_usage_status, save_snapshot, record_session, prune_session_log

DRY_RUN = False

# ---------------------------------------------------------------------------
# Paths & configuration
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
CONFIG_DIR = REPO_DIR / "config"

STATE_DIR = Path.home() / ".half-bakery"
STATE_FILE = STATE_DIR / "state.json"
LOCK_FILE = STATE_DIR / "dispatcher.lock"
CACHE_DIR = STATE_DIR / "cache"
FIELD_CACHE = CACHE_DIR / "project-fields.json"
OUTPUT_DIR = STATE_DIR / "output"
WORKTREE_DIR = STATE_DIR / "worktrees"
LOG_DIR = STATE_DIR / "logs"

# Resolve claude binary — launchd PATH is minimal, so check common locations
CLAUDE_BIN = (
    shutil.which("claude")
    or str(Path.home() / ".local" / "bin" / "claude")
)


# ---------------------------------------------------------------------------
# Concurrency budget tracker
# ---------------------------------------------------------------------------

class BudgetTracker:
    """Shared concurrency-slot tracker across dispatch phases.

    Initialized from ``len(state["running"])`` so the starting count reflects
    agents already alive.  Each phase calls ``reserve()`` before spawning and
    ``release()`` on spawn failure so the count stays in sync with
    ``state["running"]`` without either phase re-reading the dict mid-loop.

    At end-of-cycle ``tracker.count`` should equal ``len(state["running"])``.
    """

    def __init__(self, max_concurrent: int, running_count: int) -> None:
        self.max_concurrent = max_concurrent
        self._count = running_count

    def can_dispatch(self) -> bool:
        """Return True when a free concurrency slot is available."""
        return self._count < self.max_concurrent

    def reserve(self) -> None:
        """Claim one slot (call before adding to state["running"])."""
        self._count += 1

    def release(self) -> None:
        """Free one slot (call when spawn fails before the running entry is added)."""
        self._count -= 1

    @property
    def count(self) -> int:
        return self._count


def load_config():
    with open(CONFIG_DIR / "dispatcher.json") as f:
        cfg = json.load(f)
    # Expand ~ in paths
    for key in ("projects_root", "agents_root", "state_dir"):
        if key in cfg:
            cfg[key] = str(Path(cfg[key]).expanduser())
    return cfg


def load_routes():
    with open(CONFIG_DIR / "column-routes.json") as f:
        return json.load(f)


def canonical_id(issue_repo: str, issue_number: int) -> str:
    """Globally unique issue key: 'owner/repo/number'."""
    return f"{issue_repo}/{issue_number}"


def safe_id(cid: str) -> str:
    """Filename/branch-safe version of canonical_id — slashes become dashes."""
    return cid.replace("/", "-")


def resolve_project_dir(projects_root, repo_name, overrides=None):
    """Find the local directory for a GitHub repo name.

    Tries: explicit override, exact match, case-insensitive match,
    nested subdirectories (e.g., parent-dir/my-repo for repo my-repo-public).
    Returns the path string or None.
    """
    if overrides and repo_name in overrides:
        override = Path(os.path.expanduser(overrides[repo_name]))
        if override.is_dir() and (override / ".git").exists():
            return str(override)

    root = Path(projects_root)

    # Exact match
    candidate = root / repo_name
    if candidate.is_dir() and (candidate / ".git").exists():
        return str(candidate)

    # Case-insensitive match (handles "Sankey Diagramer" vs "sankey-diagramer")
    repo_lower = repo_name.lower().replace("-", " ").replace("_", " ")
    for child in root.iterdir():
        if not child.is_dir():
            continue
        child_lower = child.name.lower().replace("-", " ").replace("_", " ")
        if child_lower == repo_lower and (child / ".git").exists():
            return str(child)

    # Check with/without common suffixes (-repo, -app, -ios, -mac, -web, -api)
    for suffix in ("-repo", "-app", "-ios", "-mac", "-web", "-api"):
        stripped = repo_name.removesuffix(suffix)
        if stripped != repo_name:
            result = resolve_project_dir(projects_root, stripped, overrides)
            if result:
                return result

    # Nested: check subdirectories of each top-level dir
    # (e.g., parent-dir/my-repo/)
    for child in root.iterdir():
        if not child.is_dir() or (child / ".git").exists():
            continue  # skip git repos, only check non-repo parent dirs
        for sub in child.iterdir():
            if not sub.is_dir():
                continue
            sub_lower = sub.name.lower().replace("-", " ").replace("_", " ")
            if (sub_lower == repo_lower or sub.name == repo_name or sub.name == stripped):
                if (sub / ".git").exists():
                    return str(sub)

    return None


def get_sibling_projects(projects_root, exclude=None):
    """Return paths of git repos under projects_root, excluding named dirs."""
    exclude = exclude or []
    root = Path(projects_root)
    siblings = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if child.name in exclude:
            continue
        if (child / ".git").exists():
            siblings.append(str(child))
    return siblings


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_DIR / "dispatcher.log"),
        ],
    )


log = logging.getLogger("dispatcher")

# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"running": {}, "last_poll": None}


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    tmp.rename(STATE_FILE)


def migrate_state(state):
    """Upgrade state.json schema in-place; safe to call on every load.

    Two migration passes run unconditionally on each dispatcher boot:

    1. **Skeptic rejection counters** — moved from ``pipeline_state[cid]``
       to the top-level ``skeptic_rejections`` dict.  Counter-only entries
       (those without a ``pipeline`` key) are dropped so the pipeline reader
       never hits a KeyError.

    2. **Pipeline unification** — ``pipeline`` / ``pipeline_index`` were
       previously duplicated across both ``state["running"][cid]`` and
       ``state["pipeline_state"][cid]``, risking silent drift between the two
       copies.  ``pipeline_state`` is now the single authoritative source;
       this pass moves legacy ``pipeline`` keys out of ``running`` entries
       into ``pipeline_state`` (without overwriting an existing entry).
       All harvest and retry code paths read exclusively from ``pipeline_state``.

    Returns the mutated *state* dict (same object as input) for convenience.
    """
    # Skeptic rejection counters used to be stored inside pipeline_state[cid],
    # which caused KeyError when the pipeline reader hit a counter-only entry.
    # Move counters to their own top-level dict and drop counter-only entries.
    ps = state.setdefault("pipeline_state", {})
    rej = state.setdefault("skeptic_rejections", {})
    moved = 0
    dropped = 0
    for cid in list(ps.keys()):
        entry = ps[cid]
        if isinstance(entry, dict) and "skeptic_rejections" in entry:
            rej[cid] = entry.pop("skeptic_rejections")
            moved += 1
        if not (isinstance(entry, dict) and "pipeline" in entry):
            del ps[cid]
            dropped += 1
    if moved or dropped:
        log.info("State migration: moved %d counter(s), dropped %d counter-only entrie(s)",
                 moved, dropped)

    # Consolidate pipeline tracking to a single source of truth: pipeline_state.
    # Previously, `pipeline` / `pipeline_index` were duplicated in both
    # state["running"][cid] and state["pipeline_state"][cid], creating drift risk.
    # Move any legacy copies out of running entries into pipeline_state, which is
    # now authoritative. Harvest and retry code paths read from pipeline_state only.
    consolidated = 0
    for cid, entry in state.get("running", {}).items():
        if not isinstance(entry, dict):
            continue
        pipeline = entry.pop("pipeline", None)
        pipeline_idx = entry.pop("pipeline_index", None)
        if pipeline is not None and cid not in ps:
            # pipeline_state is authoritative — don't overwrite an existing entry.
            ps[cid] = {
                "pipeline": pipeline,
                "pipeline_index": pipeline_idx if pipeline_idx is not None else 0,
            }
            consolidated += 1
    if consolidated:
        log.info("State migration: consolidated %d running-entry pipeline(s) into pipeline_state",
                 consolidated)

    return state


# ---------------------------------------------------------------------------
# File locking
# ---------------------------------------------------------------------------

def acquire_lock():
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        log.info("Another dispatcher is running, exiting.")
        sys.exit(0)
    lock_fd.write(str(os.getpid()))
    lock_fd.flush()
    return lock_fd


def release_lock(lock_fd):
    fcntl.flock(lock_fd, fcntl.LOCK_UN)
    lock_fd.close()
    LOCK_FILE.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Process utilities
# ---------------------------------------------------------------------------

def is_pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def kill_process(pid):
    """SIGTERM, wait 10s, SIGKILL if still alive."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    for _ in range(10):
        time.sleep(1)
        if not is_pid_alive(pid):
            return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------

def gh_graphql(query, jq_filter=None, retries=3):
    cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
    if jq_filter:
        cmd.extend(["--jq", jq_filter])

    for attempt in range(1, retries + 1):
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            if jq_filter:
                return result.stdout.strip()
            return json.loads(result.stdout)

        log.warning("GraphQL attempt %d/%d failed: %s",
                    attempt, retries, result.stderr.strip())
        if attempt < retries:
            time.sleep(2)

    log.error("GraphQL failed after %d attempts", retries)
    return None


def gh_issue_comment(repo, issue_number, body):
    if DRY_RUN:
        log.info("[DRY RUN] Would comment on %s#%d", repo, issue_number)
        return
    subprocess.run(
        ["gh", "issue", "comment", str(issue_number),
         "--repo", repo, "--body", body],
        capture_output=True, text=True,
    )


def gh_issue_close(repo, issue_number):
    if DRY_RUN:
        log.info("[DRY RUN] Would close %s#%d", repo, issue_number)
        return
    subprocess.run(
        ["gh", "issue", "close", str(issue_number), "--repo", repo],
        capture_output=True, text=True,
    )


def create_followup_issues(followup_text, config, fields_cache, source_cid,
                           source_parent=None):
    """Parse FOLLOWUP/ISSUES_CREATED field and create GitHub issues on the board.

    Format (pipe-delimited, one issue per line):
        repo-name | Issue title | Brief description
    or just:
        Issue title | Brief description

    If repo-name is omitted, defaults to the source issue's repo.

    source_parent: dict with keys 'repo' and 'number' for the Epic that owns
    the source issue.  When provided, each new issue is linked as a sub-issue
    of that Epic via the addSubIssue GraphQL mutation.  Linkage failure is
    non-fatal — the issue is still created and added to the board.
    """
    if not followup_text or followup_text.strip().lower() in ("none", "n/a", ""):
        return []

    owner = config["github_repo"].split("/")[0]
    project_number = config["github_project_number"]
    default_repo = config["github_repo"]

    # Derive default repo from source CID (owner/repo/number)
    parts = source_cid.split("/")
    if len(parts) == 3:
        default_repo = f"{parts[0]}/{parts[1]}"

    # Pattern for already-created GitHub issue URLs (e.g. from Skeptic ISSUES_CREATED field).
    # These must NOT be re-created as new issues — they already exist.
    _gh_issue_url_re = re.compile(
        r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/\d+'
    )

    max_followups = config.get("max_followup_issues", 5)
    created = []
    for raw_line in followup_text.splitlines():
        if len(created) >= max_followups:
            log.warning(
                "create_followup_issues: hit cap of %d issues for %s — remaining FOLLOWUP lines ignored",
                max_followups, source_cid,
            )
            break
        # ISSUES_CREATED may be comma-separated on one line; split those too.
        for raw_item in raw_line.split(","):
            if len(created) >= max_followups:
                break
            line = raw_item.strip().strip("-").strip()
            if not line or line.lower() == "none":
                continue

            # Guard: if this entry is already a GitHub issue URL, the issue was
            # created by the agent directly (e.g. Skeptic via CLI).  Adding it to
            # `created` lets the caller report it; creating a *new* issue would
            # produce a duplicate with the URL as its title (see half-bakery#204).
            if _gh_issue_url_re.fullmatch(line):
                log.info("Skipping creation — entry is an existing issue URL: %s", line)
                created.append(line)
                # Still link to the Epic — the agent may have created this issue
                # via `gh issue create` without going through addSubIssue
                # (half-bakery#219).  Linkage failure is non-fatal.
                if source_parent and not DRY_RUN:
                    p_repo = source_parent.get("repo", "")
                    p_num = source_parent.get("number")
                    if p_repo and p_num:
                        if not link_as_sub_issue(p_repo, p_num, line):
                            log.warning(
                                "Could not link pre-existing %s as sub-issue of Epic #%d in %s "
                                "(stale parent or permission issue) — linkage skipped",
                                line, p_num, p_repo,
                            )
                continue

            segments = [s.strip() for s in line.split("|")]
            if len(segments) >= 3:
                repo_hint, title, body = segments[0], segments[1], segments[2]
                # repo_hint could be "owner/repo" or just "repo"
                repo = f"{owner}/{repo_hint}" if "/" not in repo_hint else repo_hint
            elif len(segments) == 2:
                repo = default_repo
                title, body = segments[0], segments[1]
            elif len(segments) == 1:
                repo = default_repo
                title = segments[0]
                body = f"Follow-up from {source_cid}"
            else:
                continue

            if not title:
                continue

            # Reject fragment titles — signs an agent dumped research prose into FOLLOWUP.
            # A valid issue title: ≥15 chars, starts with a capital letter or digit,
            # and doesn't begin with a lowercase continuation word.
            title_stripped = title.strip()
            if len(title_stripped) < 15:
                log.warning("Skipping FOLLOWUP with too-short title (%d chars): %r", len(title_stripped), title_stripped)
                continue
            if title_stripped[0].islower():
                log.warning("Skipping FOLLOWUP with lowercase-start title (likely a fragment): %r", title_stripped[:60])
                continue

            body_full = f"{body}\n\n_Auto-created from agent output on {source_cid}_"

            if DRY_RUN:
                log.info("[DRY RUN] Would create issue in %s: %s", repo, title)
                created.append(f"{repo}: {title}")
                continue

            result = subprocess.run(
                ["gh", "issue", "create", "--repo", repo,
                 "--title", title, "--body", body_full],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                log.warning("Failed to create follow-up issue '%s' in %s: %s",
                            title, repo, result.stderr.strip())
                continue

            issue_url = result.stdout.strip()
            log.info("Created follow-up issue: %s", issue_url)

            # Link as sub-issue BEFORE adding to the project board.
            # GitHub's addSubIssue mutation auto-enrolls the child in any project
            # the parent Epic belongs to (as a "group" sub-item with no Status).
            # By linking first, the subsequent project item-add operates on that
            # already-enrolled entry rather than creating a second duplicate item
            # that would appear as "Group selected" alongside the proper board entry.
            if source_parent:
                p_repo = source_parent.get("repo", "")
                p_num = source_parent.get("number")
                if p_repo and p_num:
                    if not link_as_sub_issue(p_repo, p_num, issue_url):
                        log.warning(
                            "Could not link %s as sub-issue of Epic #%d in %s "
                            "(stale parent or permission issue) — issue created but not nested",
                            issue_url, p_num, p_repo,
                        )

            # Add to project board (or update the auto-enrolled entry) and set Status=Ready.
            add_result = subprocess.run(
                ["gh", "project", "item-add", str(project_number),
                 "--owner", owner, "--url", issue_url, "--format", "json"],
                capture_output=True, text=True,
            )
            if add_result.returncode == 0:
                log.info("Added follow-up issue to board: %s", issue_url)
                try:
                    new_item_id = json.loads(add_result.stdout).get("id")
                except (json.JSONDecodeError, AttributeError):
                    new_item_id = None
                if new_item_id:
                    # Set Status=Ready so the dispatcher picks it up next cycle.
                    if not move_issue_to_column(fields_cache, new_item_id, "Ready"):
                        log.warning("Added %s to board but failed to set Status=Ready", issue_url)
            else:
                log.warning("Could not add %s to board: %s", issue_url, add_result.stderr.strip())

            created.append(issue_url)

    return created


# ---------------------------------------------------------------------------
# Epic / sub-issue helpers
# ---------------------------------------------------------------------------

def fetch_parent_sub_issues(repo, parent_number):
    """Fetch sub-issues of a parent issue not on the board.

    Used when a sub-issue's parent isn't in the board query results, so we
    need a targeted query to build the sibling list.
    """
    owner, name = repo.split("/", 1)
    query = f'''query {{
        repository(owner: "{owner}", name: "{name}") {{
            issue(number: {parent_number}) {{
                subIssues(first: 20) {{
                    nodes {{ number title state }}
                    totalCount
                }}
            }}
        }}
    }}'''
    data = gh_graphql(query)
    if not data:
        return []
    try:
        return data["data"]["repository"]["issue"]["subIssues"]["nodes"]
    except (KeyError, TypeError):
        return []


def fetch_epic_summary(repo, issue_number):
    """Query the sub-issue completion summary for an Epic.

    Returns a dict with keys completed/total/percent_completed, or None on error.
    """
    owner, name = repo.split("/", 1)
    query = f'''query {{
        repository(owner: "{owner}", name: "{name}") {{
            issue(number: {issue_number}) {{
                state
                subIssuesSummary {{ completed total percentCompleted }}
            }}
        }}
    }}'''
    data = gh_graphql(query)
    if not data:
        return None
    try:
        summary = data["data"]["repository"]["issue"]["subIssuesSummary"]
        return {
            "completed": summary["completed"],
            "total": summary["total"],
            "percent_completed": summary["percentCompleted"],
        }
    except (KeyError, TypeError):
        return None


def resolve_issue_node_id(repo, issue_number):
    """Return the GraphQL node ID for a GitHub issue (e.g. 'I_kwDO...' ).

    Required by addSubIssue mutation, which takes node IDs not integers.
    Returns None on failure.
    """
    owner, name = repo.split("/", 1)
    query = f'''query {{
        repository(owner: "{owner}", name: "{name}") {{
            issue(number: {issue_number}) {{
                id
            }}
        }}
    }}'''
    data = gh_graphql(query)
    if not data:
        return None
    try:
        return data["data"]["repository"]["issue"]["id"]
    except (KeyError, TypeError):
        return None


def link_as_sub_issue(parent_repo, parent_number, new_issue_url):
    """Attach new_issue_url as a sub-issue of parent_repo#parent_number.

    Calls the GitHub GraphQL addSubIssue mutation.  Failure is non-fatal —
    the issue exists even if the linkage fails; callers should warn and continue.

    Returns True if linkage succeeded, False otherwise.
    """
    # Parse new issue URL: https://github.com/owner/repo/issues/NUMBER
    url_parts = new_issue_url.rstrip("/").split("/")
    if len(url_parts) < 2:
        log.warning("link_as_sub_issue: cannot parse issue URL %s", new_issue_url)
        return False
    try:
        new_number = int(url_parts[-1])
        new_repo = f"{url_parts[-4]}/{url_parts[-3]}"
    except (ValueError, IndexError):
        log.warning("link_as_sub_issue: malformed issue URL %s", new_issue_url)
        return False

    parent_id = resolve_issue_node_id(parent_repo, parent_number)
    if not parent_id:
        log.warning("link_as_sub_issue: could not resolve node ID for %s#%d",
                    parent_repo, parent_number)
        return False

    new_issue_id = resolve_issue_node_id(new_repo, new_number)
    if not new_issue_id:
        log.warning("link_as_sub_issue: could not resolve node ID for %s#%d",
                    new_repo, new_number)
        return False

    mutation = f'''mutation {{
        addSubIssue(input: {{ issueId: "{parent_id}", subIssueId: "{new_issue_id}" }}) {{
            issue {{ number }}
            subIssue {{ number }}
        }}
    }}'''
    result = gh_graphql(mutation)
    if result is None:
        return False
    try:
        sub_num = result["data"]["addSubIssue"]["subIssue"]["number"]
        parent_num = result["data"]["addSubIssue"]["issue"]["number"]
        log.info("Linked followup #%d as sub-issue of Epic #%d", sub_num, parent_num)
        return True
    except (KeyError, TypeError) as exc:
        log.warning("link_as_sub_issue: unexpected mutation response: %s", exc)
        return False


# ---------------------------------------------------------------------------
# GitHub Projects field discovery & caching
# ---------------------------------------------------------------------------

def get_project_fields(config):
    """Discover and cache GitHub Projects field IDs and option IDs."""
    if FIELD_CACHE.exists():
        with open(FIELD_CACHE) as f:
            cached = json.load(f)
        # Cache is valid for 24 hours
        if cached.get("fetched_at"):
            fetched = datetime.fromisoformat(cached["fetched_at"])
            if (datetime.now(timezone.utc) - fetched).total_seconds() < 86400:
                return cached

    owner = config["github_repo"].split("/")[0]
    project_num = config["github_project_number"]
    query = f'''query {{
        user(login: "{owner}") {{
            projectV2(number: {project_num}) {{
                id
                fields(first: 30) {{
                    nodes {{
                        ... on ProjectV2SingleSelectField {{
                            id
                            name
                            options {{ id name }}
                        }}
                        ... on ProjectV2Field {{
                            id
                            name
                        }}
                    }}
                }}
            }}
        }}
    }}'''
    data = gh_graphql(query)
    if not data:
        log.error("Failed to fetch project fields")
        return None

    project = data["data"]["user"]["projectV2"]
    fields = {}
    for node in project["fields"]["nodes"]:
        name = node.get("name")
        if not name:
            continue
        fields[name] = {"id": node["id"]}
        if "options" in node:
            fields[name]["options"] = {
                opt["name"]: opt["id"] for opt in node["options"]
            }

    result = {
        "project_id": project["id"],
        "fields": fields,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(FIELD_CACHE, "w") as f:
        json.dump(result, f, indent=2)
    return result


# ---------------------------------------------------------------------------
# Board operations
# ---------------------------------------------------------------------------

def paginate_project_items(owner: str, project_num: int, node_fields: str) -> list:
    """Fetch all ProjectV2 items via cursor pagination, returning raw node list.

    ``node_fields`` is a plain GraphQL fragment (literal ``{`` / ``}``) for
    the per-node fields the caller cares about — everything that would appear
    inside ``nodes { ... }``.  Returns an empty list if the first request
    fails; the caller decides whether to treat that as a warning or error.
    """
    all_nodes = []
    cursor = None
    while True:
        after = f', after: "{cursor}"' if cursor else ""
        query = f'''query {{
            user(login: "{owner}") {{
                projectV2(number: {project_num}) {{
                    items(first: 100{after}) {{
                        pageInfo {{ hasNextPage endCursor }}
                        nodes {{
{node_fields}
                        }}
                    }}
                }}
            }}
        }}'''
        data = gh_graphql(query)
        if not data:
            log.warning("paginate_project_items: gh_graphql returned no data; stopping pagination")
            break
        items_data = data["data"]["user"]["projectV2"]["items"]
        all_nodes.extend(items_data["nodes"])
        if items_data["pageInfo"]["hasNextPage"]:
            cursor = items_data["pageInfo"]["endCursor"]
        else:
            break
    return all_nodes


def poll_board(config, fields_cache, routes):
    """Query the board for dispatchable issues. Paginates to get ALL items."""
    owner = config["github_repo"].split("/")[0]
    project_num = config["github_project_number"]

    node_fields = """\
                            id
                            content {
                                ... on Issue {
                                    number
                                    title
                                    state
                                    body
                                    repository { name nameWithOwner }
                                    subIssues(first: 20) {
                                        nodes { number title state }
                                        totalCount
                                    }
                                    subIssuesSummary { completed total percentCompleted }
                                    parent { number title state body repository { nameWithOwner } }
                                }
                            }
                            fieldValues(first: 15) {
                                nodes {
                                    ... on ProjectV2ItemFieldSingleSelectValue {
                                        name
                                        field { ... on ProjectV2SingleSelectField { name } }
                                    }
                                    ... on ProjectV2ItemFieldTextValue {
                                        text
                                        field { ... on ProjectV2Field { name } }
                                    }
                                }
                            }"""
    all_nodes = paginate_project_items(owner, project_num, node_fields)

    # ---- Epic-dictates-dispatch preparation ----
    # The user's prioritization rule: the Epic's Status gates all descendants.
    # If an Epic (or any ancestor) is not in "Ready", its entire subtree is
    # paused regardless of individual issue status. Orphan issues (no parent
    # Epic) are never dispatched — every story must live under an Epic.
    #
    # Build two lookups from the full board so we can walk each item's
    # ancestor chain without additional GraphQL queries.
    status_by_cid = {}   # canonical_id → current Status on the board
    parent_by_cid = {}   # canonical_id → canonical_id of immediate parent
    for node in all_nodes:
        content = node.get("content") or {}
        if not content:
            continue
        repo_full = (content.get("repository") or {}).get("nameWithOwner")
        if not repo_full or content.get("number") is None:
            continue
        cid = canonical_id(repo_full, content["number"])
        status_val = None
        for fv in node.get("fieldValues", {}).get("nodes", []):
            if (fv.get("field") or {}).get("name") == "Status" and "name" in fv:
                status_val = fv["name"]
        status_by_cid[cid] = status_val
        parent = content.get("parent")
        if parent and parent.get("number") is not None:
            p_repo = (parent.get("repository") or {}).get("nameWithOwner") or repo_full
            parent_by_cid[cid] = canonical_id(p_repo, parent["number"])

    def _ancestors_all_ready(cid, max_hops=5):
        """Return (ok, blocker_cid). ok=True iff every ancestor up the chain
        has Status=Ready. blocker_cid is the first non-Ready ancestor."""
        seen = set()
        cur = cid
        for _ in range(max_hops):
            parent_cid = parent_by_cid.get(cur)
            if parent_cid is None:
                return True, None
            if parent_cid in seen:
                return False, parent_cid  # cycle
            seen.add(parent_cid)
            if parent_cid not in status_by_cid:
                # Parent exists in GitHub's hierarchy but is not on the board —
                # no board-level gate configured for this ancestor; allow dispatch.
                return True, None
            if status_by_cid.get(parent_cid) != "Ready":
                return False, parent_cid
            cur = parent_cid
        return False, "__max_hops__"

    skipped_orphans = 0
    skipped_by_epic = {}  # {blocker_cid: count}

    dispatchable_columns = set(routes["columns"].keys()) | {"Ready"}
    items = []
    for node in all_nodes:
        content = node.get("content")
        if not content or content.get("state") != "OPEN":
            continue

        status = None
        for fv in node.get("fieldValues", {}).get("nodes", []):
            field = fv.get("field", {})
            field_name = field.get("name")
            if field_name == "Status" and "name" in fv:
                status = fv["name"]

        # Resolve target project and source repo from the issue's repository
        repo_info = content.get("repository", {})
        repo_name = repo_info.get("name")
        target_project = repo_name or "half-bakery"
        issue_repo = repo_info.get("nameWithOwner") or config["github_repo"]

        if status and status in dispatchable_columns:
            # ---- Epic-dictates-dispatch filter ----
            # Skip this filter for Epics themselves (they're containers, not
            # work units — the Epic-skip at dispatch time handles them).
            is_epic = (content.get("subIssues") or {}).get("totalCount", 0) > 0
            cid_current = canonical_id(issue_repo, content["number"])
            if not is_epic:
                if cid_current not in parent_by_cid:
                    skipped_orphans += 1
                    continue
                ok, blocker = _ancestors_all_ready(cid_current)
                if not ok:
                    skipped_by_epic[blocker] = skipped_by_epic.get(blocker, 0) + 1
                    continue
            sub_issues_data = content.get("subIssues") or {}
            summary_data = content.get("subIssuesSummary") or {}
            parent_data = content.get("parent")
            # Normalize parent: include its repo if available
            parent = None
            if parent_data:
                parent = {
                    "number": parent_data["number"],
                    "title": parent_data["title"],
                    "state": parent_data["state"],
                    "body": parent_data.get("body", ""),
                    "repo": (parent_data.get("repository") or {}).get(
                        "nameWithOwner", issue_repo
                    ),
                }
            items.append({
                "item_id": node["id"],
                "issue_number": content["number"],
                "canonical_id": canonical_id(issue_repo, content["number"]),
                "title": content["title"],
                "body": content.get("body", ""),
                "status": status,
                "target_project": target_project,
                "issue_repo": issue_repo,
                "sub_issues": sub_issues_data.get("nodes", []),
                "sub_issues_total": sub_issues_data.get("totalCount", 0),
                "sub_issues_completed": summary_data.get("completed", 0),
                "parent": parent,
            })

    # ---- Epic-gate visibility: one log line per cycle so the user can see
    # what got filtered and why (paused Epic vs. orphan). ----
    if skipped_orphans:
        log.info("Epic-gate: %d orphan issue(s) skipped — no parent Epic assigned",
                 skipped_orphans)
    for blocker_cid, count in sorted(skipped_by_epic.items(), key=lambda kv: -kv[1]):
        blocker_status = status_by_cid.get(blocker_cid, "?") if blocker_cid != "__max_hops__" else "(chain too deep)"
        log.info("Epic-gate: %d issue(s) under ancestor %s (Status=%s) — skipped",
                 count, blocker_cid, blocker_status)

    return items


def move_issue_to_column(fields_cache, item_id, column_name):
    """Move a project item to a different Status column."""
    if DRY_RUN:
        log.info("[DRY RUN] Would move item %s to %s", item_id, column_name)
        return True
    status_field = fields_cache["fields"].get("Status", {})
    field_id = status_field.get("id")
    option_id = status_field.get("options", {}).get(column_name)
    project_id = fields_cache["project_id"]

    if not field_id or not option_id:
        log.error("Cannot move to column %s: field/option not found", column_name)
        return False

    query = f'''mutation {{
        updateProjectV2ItemFieldValue(input: {{
            projectId: "{project_id}"
            itemId: "{item_id}"
            fieldId: "{field_id}"
            value: {{ singleSelectOptionId: "{option_id}" }}
        }}) {{
            projectV2Item {{ id }}
        }}
    }}'''
    result = gh_graphql(query)
    return result is not None


# ---------------------------------------------------------------------------
# Auto-routing from Ready column
# ---------------------------------------------------------------------------

def auto_route(item, routes):
    """Use Claude to classify which pipeline column an issue belongs to."""
    columns = list(routes.get("columns", {}).keys())
    descriptions = {
        "Engineering": "Build features, fix bugs, write code, implement changes, research, planning",
        "Design":      "UI/UX design, visual polish, wireframes, mockups",
        "Docs":        "Write or update documentation, README, changelog",
    }
    column_list = "\n".join(
        f"- {col}: {descriptions.get(col, routes['columns'][col].get('agent', ''))}"
        for col in columns
    )

    prompt = (
        f"You are a project issue router. Given the issue below, reply with ONLY "
        f"the column name (one of: {', '.join(columns)}). Nothing else.\n\n"
        f"Columns:\n{column_list}\n\n"
        f"Issue title: {item['title']}\n"
        f"Issue body: {item['body'][:500]}\n\n"
        f"Column:"
    )

    try:
        result = subprocess.run(
            [CLAUDE_BIN, "--print", "-p", prompt],
            capture_output=True, text=True, timeout=30,
            stdin=subprocess.DEVNULL,
        )
        answer = result.stdout.strip()
        # Extract just the column name (LLM might add extra text)
        for col in columns:
            if col.lower() in answer.lower():
                log.info("Auto-route classified issue #%d as %s",
                         item["issue_number"], col)
                return col
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("Auto-route LLM call failed: %s, falling back to Engineering", e)

    return routes.get("default_route", "Engineering")


# ---------------------------------------------------------------------------
# Git worktree management
# ---------------------------------------------------------------------------

def get_default_branch(project_dir):
    """Return the default branch name (e.g. 'main') for this repo.

    Resolves via refs/remotes/origin/HEAD; falls back to probing 'main' then
    'master' so air-gapped or newly-cloned repos without a remote tracking ref
    still work correctly.
    """
    result = subprocess.run(
        ["git", "-C", project_dir, "symbolic-ref", "refs/remotes/origin/HEAD"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        # e.g. "refs/remotes/origin/main" → "main"
        return result.stdout.strip().split("/")[-1]
    for candidate in ("main", "master"):
        check = subprocess.run(
            ["git", "-C", project_dir, "rev-parse", "--verify", candidate],
            capture_output=True, text=True,
        )
        if check.returncode == 0:
            return candidate
    return "main"


def create_worktree(project_dir, issue_number, repo_name=None):
    """Create a git worktree for this issue in the target project."""
    # Use repo_name-issue_number to avoid collisions across repos
    worktree_id = f"{repo_name}-{issue_number}" if repo_name else str(issue_number)
    worktree_path = WORKTREE_DIR / worktree_id
    branch_name = f"agent/{worktree_id}"

    if worktree_path.exists():
        log.warning("Worktree already exists at %s, cleaning up", worktree_path)
        if not cleanup_worktree(project_dir, issue_number, repo_name):
            log.error("Cannot clean stale worktree at %s, skipping issue",
                      worktree_path)
            return None
        if worktree_path.exists():
            log.error("Worktree directory still exists after cleanup: %s",
                      worktree_path)
            return None

    # Clean up orphaned branch if it exists without a worktree
    branch_check = subprocess.run(
        ["git", "-C", project_dir, "rev-parse", "--verify", branch_name],
        capture_output=True, text=True,
    )
    if branch_check.returncode == 0:
        log.warning("Orphaned branch %s exists, deleting", branch_name)
        # Prune stale worktree refs first — git refuses to delete a branch that
        # git thinks is checked out in a worktree, even with -D, even if the
        # worktree directory no longer exists.
        subprocess.run(
            ["git", "-C", project_dir, "worktree", "prune"],
            capture_output=True, text=True,
        )
        del_result = subprocess.run(
            ["git", "-C", project_dir, "branch", "-D", branch_name],
            capture_output=True, text=True,
        )
        if del_result.returncode != 0:
            log.error("Failed to delete orphaned branch %s: %s",
                      branch_name, del_result.stderr.strip())
            return None

    default_branch = get_default_branch(project_dir)
    result = subprocess.run(
        ["git", "-C", project_dir, "worktree", "add",
         str(worktree_path), "-b", branch_name, default_branch],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log.error("Failed to create worktree: %s",
                  (result.stderr or result.stdout).strip())
        # `git worktree add -b` creates the branch before the directory, so a
        # mid-command failure can leave the branch orphaned. Clean up now so
        # the next cycle doesn't waste a retry tripping over it.
        branch_check = subprocess.run(
            ["git", "-C", project_dir, "rev-parse", "--verify", branch_name],
            capture_output=True, text=True,
        )
        if branch_check.returncode == 0:
            log.warning("Cleaning up orphan branch %s left by failed worktree add", branch_name)
            subprocess.run(
                ["git", "-C", project_dir, "worktree", "prune"],
                capture_output=True, text=True,
            )
            subprocess.run(
                ["git", "-C", project_dir, "branch", "-D", branch_name],
                capture_output=True, text=True,
            )
        return None

    return str(worktree_path), branch_name


def merge_worktree(project_dir, branch_name):
    """Merge the agent branch back into the project's default branch.

    Returns (success: bool, error_reason: str or None).
    Pre-checks: aborts stale merges, verifies branch, auto-commits dirty tree.
    Config-protection: files under config/ are reset to HEAD before any
    auto-commit so the dispatcher can never silently clobber config changes
    made by agents or humans (fix for issue #117).
    Post-failure: aborts failed merge to leave repo clean.
    """
    # Pre-check 1: Abort any in-progress merge
    merge_head = Path(project_dir) / ".git" / "MERGE_HEAD"
    if merge_head.exists():
        log.warning("Aborting stale merge in %s", project_dir)
        subprocess.run(
            ["git", "-C", project_dir, "merge", "--abort"],
            capture_output=True, text=True,
        )

    # Pre-check 2: Verify the branch exists
    result = subprocess.run(
        ["git", "-C", project_dir, "rev-parse", "--verify", branch_name],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return False, f"Branch {branch_name} does not exist"

    # Pre-check 3: Ensure we're on the default branch before merging.
    # Merging into a feature branch causes conflicts when both the feature branch
    # and the agent branch modified the same files.
    # See: https://github.com/justintormey/half-bakery/issues/193
    default_branch = get_default_branch(project_dir)
    current_branch_result = subprocess.run(
        ["git", "-C", project_dir, "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    )
    current_branch = current_branch_result.stdout.strip()
    if current_branch != default_branch:
        status_check = subprocess.run(
            ["git", "-C", project_dir, "status", "--porcelain"],
            capture_output=True, text=True,
        )
        dirty_lines = [l for l in status_check.stdout.splitlines() if not l.startswith("??")]
        if dirty_lines:
            log.warning(
                "Project dir %s is on branch '%s' with %d dirty file(s) — stashing before "
                "switching to '%s'. Commit these changes manually if they are intentional.",
                project_dir, current_branch, len(dirty_lines), default_branch,
            )
            subprocess.run(
                ["git", "-C", project_dir, "stash", "push", "-u", "-m",
                 f"dispatcher: stashed {current_branch} before merge to {default_branch}"],
                capture_output=True, text=True,
            )
        else:
            log.warning(
                "Project dir %s is on branch '%s', not '%s' — switching before merge",
                project_dir, current_branch, default_branch,
            )
        checkout_result = subprocess.run(
            ["git", "-C", project_dir, "checkout", default_branch],
            capture_output=True, text=True,
        )
        if checkout_result.returncode != 0:
            return False, (
                f"Failed to checkout {default_branch}: {checkout_result.stderr.strip()}"
            )

    # Pre-check 4: Auto-commit dirty working tree so merges start clean.
    # IMPORTANT: Config files are protected — if any file under config/ is dirty,
    # we reset it to HEAD before committing.  Auto-commits must never clobber
    # intentional architectural decisions made by agents or humans.
    # See: https://github.com/justintormey/half-bakery/issues/117
    status_result = subprocess.run(
        ["git", "-C", project_dir, "status", "--porcelain"],
        capture_output=True, text=True,
    )
    if status_result.stdout.strip():
        # Identify dirty tracked files under config/ and reset them to HEAD.
        # Untracked files (status "??") are excluded: git checkout HEAD silently
        # does nothing for them, and they would then be staged by git add -A.
        # See: https://github.com/justintormey/half-bakery/issues/121
        dirty_config_files = [
            line[3:]  # strip the two-char status prefix + space
            for line in status_result.stdout.splitlines()
            if line[3:].startswith("config/") and not line.startswith("??")
        ]
        if dirty_config_files:
            log.warning(
                "Auto-commit protection: resetting %d config file(s) to HEAD in %s "
                "to prevent clobbering intentional config changes: %s",
                len(dirty_config_files), project_dir, dirty_config_files,
            )
            reset_result = subprocess.run(
                ["git", "-C", project_dir, "checkout", "HEAD", "--"] + dirty_config_files,
                capture_output=True, text=True,
            )
            if reset_result.returncode != 0:
                log.error(
                    "Config-protection reset failed (rc=%d) in %s: %s — aborting merge "
                    "to prevent clobbering protected config files.",
                    reset_result.returncode, project_dir, reset_result.stderr.strip(),
                )
                return False, (
                    f"Config-protection reset failed (rc={reset_result.returncode}): "
                    f"{reset_result.stderr.strip()}"
                )

        # Re-check after resetting protected files — may be clean now
        status_result2 = subprocess.run(
            ["git", "-C", project_dir, "status", "--porcelain"],
            capture_output=True, text=True,
        )
        if status_result2.stdout.strip():
            log.info("Auto-committing dirty working tree in %s before merge", project_dir)
            subprocess.run(
                ["git", "-C", project_dir, "add", "-A"],
                capture_output=True, text=True,
            )
            subprocess.run(
                ["git", "-C", project_dir, "commit", "-m",
                 "chore(dispatcher): auto-commit dirty tree before merge"],
                capture_output=True, text=True,
            )

    # Attempt the merge — retry once on macOS EDEADLK (transient VM lock failure)
    import time as _time
    for attempt in range(2):
        result = subprocess.run(
            ["git", "-C", project_dir, "merge", branch_name, "--no-edit"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            return True, None

        # git writes merge conflict details to stdout ("CONFLICT (content): ...",
        # "Automatic merge failed; fix conflicts...") and errors to stderr.
        # Combine both so the log is actually useful for debugging.
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        error_msg = " | ".join(p for p in (stderr, stdout) if p) or f"exit={result.returncode} (no output)"
        log.error("Merge failed for %s (attempt %d): %s", branch_name, attempt + 1, error_msg)

        subprocess.run(
            ["git", "-C", project_dir, "merge", "--abort"],
            capture_output=True, text=True,
        )

        # EDEADLK: macOS post-wake transient VM lock — wait and retry
        if "deadlock" in error_msg.lower() or "ORIG_HEAD" in error_msg:
            if attempt == 0:
                log.warning("EDEADLK on merge for %s — retrying in 15s", branch_name)
                _time.sleep(15)
                continue
        break

    return False, error_msg


def cleanup_worktree(project_dir, issue_number, repo_name=None):
    """Remove the worktree and delete the branch. Always succeeds or returns False."""
    worktree_id = f"{repo_name}-{issue_number}" if repo_name else str(issue_number)
    worktree_path = WORKTREE_DIR / worktree_id
    branch_name = f"agent/{worktree_id}"

    if worktree_path.exists():
        # Tier 1: Ask git to remove it cleanly
        result = subprocess.run(
            ["git", "-C", project_dir, "worktree", "remove",
             str(worktree_path), "--force"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            log.warning("git worktree remove failed for %s: %s",
                        worktree_path, result.stderr.strip())
            # Tier 2: Nuclear — rm the directory, then prune stale refs
            try:
                shutil.rmtree(worktree_path)
                log.info("Removed worktree directory manually: %s", worktree_path)
            except FileNotFoundError:
                # git's `worktree remove` actually deletes the working dir first,
                # then fails on its metadata ref (e.g., EINTR on the lock file).
                # The directory is already gone — `git worktree prune` below
                # handles the stale metadata. This is a success, not an error.
                log.info("Worktree directory already removed by git: %s", worktree_path)
            except OSError as e:
                log.error("Failed to rm worktree directory %s: %s", worktree_path, e)
                return False

    # Always prune to clean up stale worktree references
    subprocess.run(
        ["git", "-C", project_dir, "worktree", "prune"],
        capture_output=True, text=True,
    )

    # Delete the branch (ignore errors if already deleted)
    subprocess.run(
        ["git", "-C", project_dir, "branch", "-D", branch_name],
        capture_output=True, text=True,
    )
    return True


# ---------------------------------------------------------------------------
# Agent spawning
# ---------------------------------------------------------------------------

def spawn_agent(config, routes, item, worktree_path):
    """Spawn a claude CLI process for this issue."""
    agent_type = routes["columns"][item["status"]]["agent"]
    agents_root = Path(config["agents_root"])
    agents_md = agents_root / agent_type / "AGENTS.md"

    if not agents_md.exists():
        log.error("Agent file not found: %s", agents_md)
        return None

    system_prompt = agents_md.read_text()

    # Detect spanning project
    is_spanning = item.get("target_project") in config.get("spanning_projects", [])
    sibling_projects = []
    if is_spanning:
        sibling_projects = get_sibling_projects(
            config["projects_root"],
            exclude=[item["target_project"]],
        )
        log.info("Issue #%d: spanning mode — adding %d sibling project dirs",
                 item["issue_number"], len(sibling_projects))

    if is_spanning and sibling_projects:
        work_scope = (f"Your primary working directory is {worktree_path}/, "
                      f"but you also have access to the sibling projects listed below.")
    else:
        work_scope = f"Work ONLY in your assigned directory: {worktree_path}/"

    assignment = f"""## Your Assignment

**Issue:** {item['canonical_id']} — {item['title']}
**GitHub:** https://github.com/{item['issue_repo']}/issues/{item['issue_number']}
**Project:** {item.get('target_project', 'unknown')}
**Working directory:** {worktree_path}/
**Pipeline stage:** {item['status']}

## Operational Constraints
You are a headless agent. Follow these rules:
- Do NOT run /login, /memory, or any slash commands
- Do NOT use these skills (they spawn processes or require human interaction):
  schedule, loop, claude-session-driver, commit-push-pr, dispatching-parallel-agents,
  subagent-driven-development, using-git-worktrees, finishing-a-development-branch,
  requesting-code-review, receiving-code-review, writing-skills, writing-plans,
  executing-plans, brainstorming, using-superpowers
- You MAY use: systematic-debugging, episodic-memory:remembering-conversations,
  planning-with-files:plan, verification-before-completion, simplify
- Execute the assignment directly. Do not ask clarifying questions — document
  ambiguity and proceed with the most reasonable interpretation.
- These rules override any proactive instructions in CLAUDE.md

## Issue Details
{item['body']}

## Instructions
1. {work_scope}
2. Read the project's CLAUDE.md or history.md if it exists before starting.
3. Read half-bakery/docs/project-visions.md for the owner's vision and priorities.
4. Do the work described in the issue.
5. ALL output (code, docs, research, architecture, QA reports, analysis) goes in
   YOUR PROJECT'S directory under a `.agent/` folder — NEVER in the half-bakery repo.
   Example paths: `.agent/qa-report.md`, `.agent/research.md`, `.agent/architecture.md`
   The half-bakery repo is infrastructure only. The ONLY exception is
   half-bakery/docs/project-visions.md which you may READ but never WRITE.
6. Commit your changes with clear messages referencing issue #{item['issue_number']} ({item['issue_repo']}).
7. If you create any GitHub issues, immediately add each one to the project board:
   gh project item-add {config['github_project_number']} --owner {config['github_repo'].split('/')[0]} --url <issue-url>
   Always pass --repo {item['issue_repo']} when using `gh issue` commands for this issue.
8. If you are blocked and cannot complete the work, output a line starting
   with "##BLOCKED##" followed by what's blocking you.
9. When done, output this EXACT block (fill in each field):
   ##SUMMARY##
   DONE: <one sentence of what was accomplished>
   FILES: <comma-separated list of changed files>
   COMMITS: <commit SHAs or "none">
   FOLLOWUP: <pipe-delimited follow-up issues, one per line: "repo-name | Issue title | Brief description", or "none">
   ##END##
   For FOLLOWUP, use the short repo name (e.g. "vibecheck-app", "runbook"). The dispatcher will
   auto-create these issues on GitHub and add them to the board. Only list genuinely new work
   that emerged from your findings — not work already captured in existing issues.
"""

    if is_spanning and sibling_projects:
        project_list = "\n".join(f"- {p}" for p in sibling_projects)
        assignment += f"""
## Available Projects
You have direct access to the following project directories.
You may read, modify, and commit in any of them:
{project_list}
"""

    # Inject project-specific gotchas from history.md "Important Notes" section.
    history_path = Path(worktree_path) / "history.md"
    if history_path.exists():
        history_text = history_path.read_text()
        if "## Important Notes" in history_text:
            notes = history_text.split("## Important Notes")[1].split("\n##")[0].strip()
            if notes:
                assignment += f"\n## Project Notes (from history.md)\n{notes}\n"

    # Retry context: inject failure info so agent doesn't repeat mistakes
    if item.get("retry_context"):
        ctx = item["retry_context"]
        assignment += f"""
## Prior Attempt Failed (Retry {ctx['retry_count']})
**Reason:** {ctx['prior_failure']}
Do NOT repeat the same mistake. Address the issue directly.
Read the evaluation failure reason carefully and correct your approach.
"""

    # Epic context enrichment: inject parent Epic info for sub-issues.
    if item.get("parent"):
        parent = item["parent"]
        assignment += f"""
## Epic Context
This issue is a sub-issue (story/task) of a larger Epic.

**Parent Epic:** #{parent['number']} — {parent['title']}
**Epic Description:**
{parent.get('body', '(no description)')}
"""
        siblings = item.get("siblings", [])
        if siblings:
            sibling_list = "\n".join(
                f"- #{s['number']} {s['title']} — **{s['state']}**"
                for s in siblings
            )
            assignment += f"""
**Related sub-issues (siblings):**
{sibling_list}

Focus on YOUR assigned sub-issue. The sibling list is for context only —
do not attempt to do work assigned to other sub-issues.
"""

    output_file = OUTPUT_DIR / f"{safe_id(item['canonical_id'])}.log"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if DRY_RUN:
        log.info("[DRY RUN] Would spawn %s for issue #%d in %s",
                 agent_type, item["issue_number"], worktree_path)
        return None

    # Select model per agent type (default: sonnet)
    agent_models = config.get("agent_models", {})
    model = agent_models.get(agent_type, "sonnet")

    cmd = [
        CLAUDE_BIN, "--print",
        "--output-format", "json",
        "--model", model,
        "--system-prompt", system_prompt,
        "--append-system-prompt", assignment,
        "--add-dir", worktree_path,
    ]

    # Spanning projects get --add-dir for all sibling repos
    for proj_path in sibling_projects:
        cmd.extend(["--add-dir", proj_path])

    cmd.extend([
        "--permission-mode", config.get("claude_permission_mode", "acceptEdits"),
        "--name", f"half-bakery-{safe_id(item['canonical_id'])}-{agent_type}",
        "Execute the assignment above.",
    ])

    log.info("Agent %s using model: %s", agent_type, model)

    try:
        with open(output_file, "w") as out:
            proc = subprocess.Popen(
                cmd, stdout=out, stderr=subprocess.STDOUT,
                cwd=worktree_path,
                start_new_session=True,
                stdin=subprocess.DEVNULL,
            )
    except OSError as e:
        log.error("Failed to spawn agent for issue #%d: %s",
                  item["issue_number"], e)
        return None

    log.info("Spawned agent %s for issue #%d (PID %d)",
             agent_type, item["issue_number"], proc.pid)
    return proc.pid


def spawn_local_agent(config, provider, routes, item, worktree_path):
    """Spawn a local LLM agent via local_agent.py for this issue.

    Uses the same assignment prompt as spawn_agent() but routes to a local
    llama-server via the OpenAI-compatible API instead of the claude CLI.
    Returns None (triggering fallback) if the server is unreachable.
    """
    # Health check: is the local server reachable?
    base_url = provider.get("base_url", "")
    if base_url:
        import urllib.request
        try:
            health_url = base_url.rstrip("/").rsplit("/v1", 1)[0] + "/health"
            req = urllib.request.Request(health_url, method="GET")
            urllib.request.urlopen(req, timeout=3)
        except Exception:
            log.info("Local provider at %s is unreachable, skipping", base_url)
            return None

    agent_type = routes["columns"][item["status"]]["agent"]
    agents_root = Path(config["agents_root"])
    agents_md = agents_root / agent_type / "AGENTS.md"

    if not agents_md.exists():
        log.error("Agent file not found: %s", agents_md)
        return None

    # Build the same assignment prompt as spawn_agent
    is_spanning = item.get("target_project") in config.get("spanning_projects", [])
    sibling_projects = []
    if is_spanning:
        sibling_projects = get_sibling_projects(
            config["projects_root"],
            exclude=[item["target_project"]],
        )

    if is_spanning and sibling_projects:
        work_scope = (f"Your primary working directory is {worktree_path}/, "
                      f"but you also have access to the sibling projects listed below.")
    else:
        work_scope = f"Work ONLY in your assigned directory: {worktree_path}/"

    assignment = f"""## Your Assignment

**Issue:** {item['canonical_id']} — {item['title']}
**GitHub:** https://github.com/{item['issue_repo']}/issues/{item['issue_number']}
**Project:** {item.get('target_project', 'unknown')}
**Working directory:** {worktree_path}/
**Pipeline stage:** {item['status']}

## Operational Constraints
You are a headless agent running on a local LLM. Follow these rules:
- Execute the assignment directly. Do not ask clarifying questions.
- Use the provided tools (read_file, write_file, edit_file, bash, grep, glob) to do your work.
- Commit your changes using the bash tool with git commands.
- All file paths should be absolute paths.

## Issue Details
{item['body']}

## Instructions
1. {work_scope}
2. Read the project's CLAUDE.md or history.md if it exists before starting.
3. Read half-bakery/docs/project-visions.md for the owner's vision and priorities.
4. Do the work described in the issue.
5. ALL output (code, docs, research, architecture, QA reports, analysis) goes in
   YOUR PROJECT'S directory under a `.agent/` folder — NEVER in the half-bakery repo.
   Example paths: `.agent/qa-report.md`, `.agent/research.md`, `.agent/architecture.md`
   The half-bakery repo is infrastructure only. The ONLY exception is
   half-bakery/docs/project-visions.md which you may READ but never WRITE.
6. Commit your changes with clear messages referencing issue #{item['issue_number']} ({item['issue_repo']}).
7. If you create any GitHub issues, immediately add each one to the project board:
   gh project item-add {config['github_project_number']} --owner {config['github_repo'].split('/')[0]} --url <issue-url>
   Always pass --repo {item['issue_repo']} when using `gh issue` commands for this issue.
8. If you are blocked and cannot complete the work, output a line starting
   with "##BLOCKED##" followed by what's blocking you.
9. When done, output this EXACT block (fill in each field):
   ##SUMMARY##
   DONE: <one sentence of what was accomplished>
   FILES: <comma-separated list of changed files>
   COMMITS: <commit SHAs or "none">
   FOLLOWUP: <pipe-delimited follow-up issues, one per line: "repo-name | Issue title | Brief description", or "none">
   ##END##
   For FOLLOWUP, use the short repo name (e.g. "vibecheck-app", "runbook"). The dispatcher will
   auto-create these issues on GitHub and add them to the board. Only list genuinely new work
   that emerged from your findings — not work already captured in existing issues.
"""

    if is_spanning and sibling_projects:
        project_list = "\n".join(f"- {p}" for p in sibling_projects)
        assignment += f"""
## Available Projects
You have direct access to the following project directories.
You may read, modify, and commit in any of them:
{project_list}
"""

    # Retry context
    if item.get("retry_context"):
        ctx = item["retry_context"]
        assignment += f"""
## Prior Attempt Failed (Retry {ctx['retry_count']})
**Reason:** {ctx['prior_failure']}
Do NOT repeat the same mistake. Address the issue directly.
"""

    output_file = OUTPUT_DIR / f"{safe_id(item['canonical_id'])}.log"
    stderr_file = OUTPUT_DIR / f"{safe_id(item['canonical_id'])}.stderr.log"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if DRY_RUN:
        log.info("[DRY RUN] Would spawn local agent %s for issue #%d in %s",
                 agent_type, item["issue_number"], worktree_path)
        return None

    local_agent_script = SCRIPT_DIR / "local_agent.py"
    if not local_agent_script.exists():
        log.error("local_agent.py not found at %s", local_agent_script)
        return None

    cmd = [
        sys.executable, str(local_agent_script),
        "--base-url", provider["base_url"],
        "--model", provider.get("model", "default"),
        "--persona", str(agents_md),
        "--assignment", assignment,
        "--workdir", worktree_path,
        "--max-turns", str(provider.get("max_turns", 50)),
        "--ctx-size", str(provider.get("ctx_size", 65536)),
        "--verbose",
    ]

    log.info("Local agent %s for issue #%d → %s (model: %s)",
             agent_type, item["issue_number"],
             provider["base_url"], provider.get("model", "default"))

    try:
        # stdout → output_file (JSON only — parsed by phase_harvest)
        # stderr → stderr_file (verbose progress logs — for debugging)
        # Keeping them separate prevents verbose output from corrupting json.loads()
        with open(output_file, "w") as out, open(stderr_file, "w") as err:
            proc = subprocess.Popen(
                cmd, stdout=out, stderr=err,
                cwd=worktree_path,
                start_new_session=True,
                stdin=subprocess.DEVNULL,
            )
    except OSError as e:
        log.error("Failed to spawn local agent for issue #%d: %s",
                  item["issue_number"], e)
        return None

    log.info("Spawned local agent %s for issue #%d (PID %d)",
             agent_type, item["issue_number"], proc.pid)
    return proc.pid


def spawn_for_provider(config, routes, item, worktree_path):
    """Spawn agent using the configured provider, with fallback to claude CLI.

    Checks column-routes.json for a per-column 'provider' override, then
    falls back to config['default_provider'], then to 'claude'.
    """
    column = item["status"]
    route = routes["columns"].get(column, {})
    provider_name = route.get("provider", config.get("default_provider", "claude"))
    providers = config.get("providers", {})
    provider = providers.get(provider_name, {})

    if provider.get("type") == "local":
        log.info("Issue #%d: using local provider '%s' for column '%s'",
                 item["issue_number"], provider_name, column)
        pid = spawn_local_agent(config, provider, routes, item, worktree_path)
        if pid is not None:
            return pid, provider_name

        # Local spawn failed — fall back to the configured fallback provider.
        # Only "claude" is supported as a fallback; guard against misconfiguration.
        fallback_name = provider.get("fallback_provider", "claude")
        if fallback_name != "claude":
            log.error(
                "Unsupported fallback_provider '%s' for issue #%d — "
                "only 'claude' is supported. Defaulting to 'claude'.",
                fallback_name, item["issue_number"],
            )
            fallback_name = "claude"
        log.warning("Local agent spawn failed for #%d, falling back to '%s'",
                    item["issue_number"], fallback_name)
        pid = spawn_agent(config, routes, item, worktree_path)
        return pid, fallback_name
    else:
        pid = spawn_agent(config, routes, item, worktree_path)
        return pid, "claude"


# ---------------------------------------------------------------------------
# Dispatcher phases
# ---------------------------------------------------------------------------

def phase_timeout_check(state, config, fields_cache):
    """Kill agents that have exceeded the timeout."""
    timeout_minutes = config.get("agent_timeout_minutes", 30)
    now = datetime.now(timezone.utc)

    for cid, info in list(state["running"].items()):
        issue_number = int(cid.split("/")[-1])
        started = datetime.fromisoformat(info["started"])
        elapsed = (now - started).total_seconds() / 60

        if elapsed > timeout_minutes:
            log.warning("Issue %s timed out after %.0f minutes, killing PID %d",
                        cid, elapsed, info["pid"])
            kill_process(info["pid"])

            repo = info.get("issue_repo", config["github_repo"])
            gh_issue_comment(repo, issue_number,
                f"**Agent timed out** after {elapsed:.0f} minutes "
                f"(limit: {timeout_minutes}m). Moving to Review.\n\n"
                f"Agent type: `{info['agent']}`\n"
                f"Worktree branch `{info.get('branch', 'unknown')}` preserved for inspection.")
            move_issue_to_column(fields_cache, info.get("item_id", ""), "Review")

            # Preserve worktree for inspection on timeout
            del state["running"][cid]
            save_state(state)


def phase_retry_queue(state, config, fields_cache, routes, budget: "BudgetTracker"):
    """Re-dispatch issues from the retry queue with failure context."""
    retry_queue = state.get("retry_queue", {})
    if not retry_queue:
        return

    for cid, retry_info in list(retry_queue.items()):
        issue_number = int(cid.split("/")[-1])
        if not budget.can_dispatch():
            break

        log.info("Retrying issue %s (attempt %d, prior: %s)",
                 cid, retry_info["retry_count"], retry_info["prior_failure"])

        target_project = retry_info.get("target_project", "")
        project_dir = resolve_project_dir(config["projects_root"], target_project,
                                          config.get("project_overrides"))
        if not project_dir:
            log.warning("Retry: project dir not found for %s, moving to Review", target_project)
            move_issue_to_column(fields_cache, retry_info.get("item_id", ""), "Review")
            del retry_queue[cid]
            continue

        result = create_worktree(project_dir, issue_number, target_project)
        if not result:
            log.error("Retry: failed to create worktree for %s", cid)
            del retry_queue[cid]
            continue
        worktree_path, branch_name = result

        # Build a synthetic item for spawn_agent with retry context
        item = {
            "issue_number": issue_number,
            "canonical_id": cid,
            "title": retry_info.get("title", ""),
            "body": retry_info.get("body", ""),
            "status": retry_info.get("column", "Engineering"),
            "target_project": target_project,
            "issue_repo": retry_info.get("issue_repo", config["github_repo"]),
            "item_id": retry_info.get("item_id", ""),
            "parent": retry_info.get("parent_issue"),
            "retry_context": {
                "retry_count": retry_info["retry_count"],
                "prior_failure": retry_info["prior_failure"],
            },
        }

        # If force_provider is set (e.g., local agent failed), bypass routing
        budget.reserve()
        if retry_info.get("force_provider") == "claude":
            log.info("Issue %s: forced provider 'claude' on retry", cid)
            pid = spawn_agent(config, routes, item, worktree_path)
            used_provider = "claude"
        else:
            pid, used_provider = spawn_for_provider(config, routes, item, worktree_path)
        if pid is None:
            budget.release()
            cleanup_worktree(project_dir, issue_number, target_project)
            del retry_queue[cid]
            continue

        entry = {
            "agent": routes["columns"][item["status"]]["agent"],
            "pid": pid,
            "started": datetime.now(timezone.utc).isoformat(),
            "project": target_project,
            "column": item["status"],
            "worktree": worktree_path,
            "branch": branch_name,
            "item_id": item["item_id"],
            "issue_repo": item["issue_repo"],
            "retry_count": retry_info["retry_count"],
            "title": item["title"],
            "body": item["body"],
            "provider": used_provider,
        }
        # pipeline/pipeline_index live in state["pipeline_state"][cid] — already
        # persisted by the prior harvest cycle that enqueued this retry.
        if retry_info.get("parent_issue"):
            entry["parent_issue"] = retry_info["parent_issue"]
        state["running"][cid] = entry
        del retry_queue[cid]
        log.info("Retried issue %s (PID %d, provider: %s)", cid, pid, used_provider)

    save_state(state)


def phase_poll_and_dispatch(state, config, fields_cache, routes, budget: "BudgetTracker"):
    """Poll the board and dispatch agents for ready issues.

    v3: Budget-aware concurrency, pipeline classification, big-build deferral.
    """
    cfg_budget = get_budget_profile(config)

    # Usage-aware throttling: check actual consumption
    usage = get_usage_status()
    if usage["should_pause"]:
        log.warning("Usage: PAUSED — %s", usage["reason"])
        return
    if usage["should_throttle"]:
        budget.max_concurrent = max(1, budget.max_concurrent - 1)
        log.info("Usage: throttled to max_concurrent=%d — %s",
                 budget.max_concurrent, usage["reason"])

    log.info("Budget: %s | window=%s%% (%d/%d output tokens, %d sessions)",
             get_budget_summary(state, config), usage["window_pct"],
             usage["window_output_tokens"], usage["window_output_ceiling"],
             usage["window_sessions"])

    # budget.can_dispatch() reflects slots already consumed by phase_retry_queue
    # this cycle, so no need to re-read len(state["running"]) here.
    if not budget.can_dispatch():
        log.info("At max concurrency (%d/%d), skipping poll",
                 budget.count, budget.max_concurrent)
        return

    items = poll_board(config, fields_cache, routes)

    # --- Sibling resolution (Step 6 of epic/sub-issue design) ---
    # Build a map of epic_number -> sub_issues list from Epic items on the board.
    # This is used to enrich sub-issue assignments with their siblings list.
    epic_sub_issues: dict = {}
    for item in items:
        if item.get("sub_issues_total", 0) > 0:
            epic_sub_issues[item["issue_number"]] = item["sub_issues"]

    # For sub-issues whose parent Epic is NOT on the board, fetch via API.
    # Cache the results to avoid duplicate calls for siblings sharing a parent.
    off_board_parent_cache: dict = {}
    for item in items:
        parent = item.get("parent")
        if not parent:
            continue
        pnum = parent["number"]
        if pnum not in epic_sub_issues and pnum not in off_board_parent_cache:
            parent_repo = parent.get("repo", item["issue_repo"])
            off_board_parent_cache[pnum] = fetch_parent_sub_issues(
                parent_repo, pnum
            )

    # Populate siblings on each sub-issue item (everyone with a parent).
    for item in items:
        parent = item.get("parent")
        if not parent:
            continue
        pnum = parent["number"]
        all_siblings = (
            epic_sub_issues.get(pnum) or off_board_parent_cache.get(pnum) or []
        )
        # Exclude self from the sibling list.
        item["siblings"] = [
            s for s in all_siblings if s["number"] != item["issue_number"]
        ]

    running_issues = set(state["running"].keys())

    # Prioritize Ready items over mid-pipeline items so the board's priority
    # order is respected. Within each group, board order (from poll_board) is
    # preserved. Without this, a single issue cycling through a long pipeline
    # monopolizes the concurrency slot and blocks top-of-backlog work.
    items = sorted(items, key=lambda x: 0 if x["status"] == "Ready" else 1)

    for item in items:
        if item["canonical_id"] in running_issues:
            continue
        if not budget.can_dispatch():
            break

        # Skip Epics — they are containers; only sub-issues get dispatched.
        if item.get("sub_issues_total", 0) > 0:
            log.info(
                "Issue #%d is an Epic (%d sub-issues, %d completed) — skipping dispatch",
                item["issue_number"],
                item["sub_issues_total"],
                item["sub_issues_completed"],
            )
            continue

        # Skip items in non-dispatchable columns (Done, Review, Backlog)
        non_dispatchable = set(routes.get("non_dispatchable", ["Review", "Done", "Backlog"]))
        if item["status"] in non_dispatchable:
            continue

        # Skip items whose status isn't a known column (safety guard)
        if item["status"] != "Ready" and item["status"] not in routes["columns"]:
            log.warning("Issue #%d has unknown status '%s', skipping",
                        item["issue_number"], item["status"])
            continue

        # Budget: defer big builds to off-hours
        if should_defer_issue(item, cfg_budget):
            log.info("Deferring big build issue #%d until aggressive mode",
                     item["issue_number"])
            continue

        # Auto-route from Ready with pipeline classification
        if item["status"] == "Ready":
            # Check if this issue has existing pipeline state (was routed back by Skeptic)
            pipeline_state = state.get("pipeline_state", {}).get(item["canonical_id"])
            if pipeline_state and "pipeline" in pipeline_state:
                pipeline = pipeline_state["pipeline"]
                pipeline_idx = pipeline_state["pipeline_index"]
                target_column = pipeline[pipeline_idx] if pipeline_idx < len(pipeline) else "Engineering"
                log.info("Resuming issue #%d at pipeline[%d]=%s (pipeline: %s)",
                         item["issue_number"], pipeline_idx, target_column,
                         " → ".join(pipeline))
            else:
                issue_type, pipeline = classify_issue(item["title"], item["body"])
                target_column = pipeline[0]
                pipeline_idx = 0
                log.info("Auto-routing issue #%d as '%s' → pipeline %s",
                         item["issue_number"], issue_type, " → ".join(pipeline))

            # If pipeline says "Done", close the issue instead of dispatching
            if target_column == "Done":
                log.info("Pipeline complete for issue #%d — closing", item["issue_number"])
                move_issue_to_column(fields_cache, item["item_id"], "Done")
                repo = item.get("issue_repo", config["github_repo"])
                gh_issue_close(repo, item["issue_number"])
                # Clean up pipeline state, rejection counter, and merge-retry counter
                state.get("pipeline_state", {}).pop(item["canonical_id"], None)
                state.get("skeptic_rejections", {}).pop(item["canonical_id"], None)
                state.get("merge_retries", {}).pop(item["canonical_id"], None)
                continue

            # Persist the pipeline assignment to pipeline_state (single source of truth).
            # Harvest and retry paths read pipeline info from here — the running entry
            # itself no longer carries pipeline/pipeline_index fields.
            state.setdefault("pipeline_state", {})[item["canonical_id"]] = {
                "pipeline": pipeline,
                "pipeline_index": pipeline_idx,
            }

            move_issue_to_column(fields_cache, item["item_id"], target_column)
            item["status"] = target_column

        # Validate target project directory
        target_project = item["target_project"]
        project_dir = resolve_project_dir(config["projects_root"], target_project,
                                          config.get("project_overrides"))
        if not project_dir:
            log.warning("Project directory for '%s' not found under %s, skipping issue #%d",
                        target_project, config["projects_root"], item["issue_number"])
            continue

        # Create worktree
        result = create_worktree(project_dir, item["issue_number"], target_project)
        if not result:
            log.error("Failed to create worktree for %s", item["canonical_id"])
            continue
        worktree_path, branch_name = result

        # Spawn agent (routes through provider config)
        budget.reserve()
        pid, used_provider = spawn_for_provider(config, routes, item, worktree_path)
        if pid is None:
            budget.release()
            cleanup_worktree(project_dir, item["issue_number"], target_project)
            continue

        entry = {
            "agent": routes["columns"][item["status"]]["agent"],
            "pid": pid,
            "started": datetime.now(timezone.utc).isoformat(),
            "project": target_project,
            "column": item["status"],
            "worktree": worktree_path,
            "branch": branch_name,
            "item_id": item["item_id"],
            "issue_repo": item.get("issue_repo", config["github_repo"]),
            "title": item.get("title", ""),
            "body": item.get("body", "")[:500],  # truncate for state file size
            "provider": used_provider,
        }
        # (pipeline/pipeline_index live in state["pipeline_state"][cid] now — no duplication here)
        # Store parent Epic reference so harvest can post progress updates.
        if item.get("parent"):
            entry["parent_issue"] = item["parent"]
        state["running"][item["canonical_id"]] = entry
        running_issues.add(item["canonical_id"])   # prevent duplicate dispatch same cycle
        log.info("Dispatched %s to %s (PID %d)",
                 item["canonical_id"], item["status"], pid)


def phase_harvest(state, config, fields_cache, routes):
    """Check for finished agents and evaluate their output smartly.

    Uses layered evaluation gates (evaluator.py) instead of the old
    ">50 chars = done" heuristic. Failed evaluations trigger retries
    with failure context before giving up.
    """

    for cid, info in list(state["running"].items()):
        issue_number = int(cid.split("/")[-1])
        try:
            if is_pid_alive(info["pid"]):
                continue

            repo = info.get("issue_repo", config["github_repo"])
            output_file = OUTPUT_DIR / f"{safe_id(cid)}.log"
            stderr_file = OUTPUT_DIR / f"{safe_id(cid)}.stderr.log"
            raw_output = ""
            if output_file.exists():
                raw_output = output_file.read_text()

            project_dir = str(Path(config["projects_root"]) / info.get("project", ""))

            # Parse JSON output to extract both result text and usage data.
            # With --output-format json, the output is a JSON object with
            # "result" (the text) and "usage" (token counts).
            output_text = raw_output
            session_usage = {}
            try:
                json_output = json.loads(raw_output)
                output_text = json_output.get("result", raw_output)
                session_usage = json_output.get("usage", {})
                session_usage["total_cost_usd"] = json_output.get("total_cost_usd", 0)
                session_usage["duration_ms"] = json_output.get("duration_ms", 0)
            except (json.JSONDecodeError, TypeError):
                # Not JSON — agent may have crashed before producing structured output
                pass

            # Record per-session token usage for rolling window tracking
            if session_usage:
                record_session(session_usage, info["agent"], issue_number)

            # Track session stats for budget (legacy rolling average)
            started = datetime.fromisoformat(info["started"])
            duration_min = (datetime.now(timezone.utc) - started).total_seconds() / 60
            update_session_stats(state, info["agent"], duration_min, len(output_text))

            log.info("Issue %s: agent finished (PID %d, %.0f min, provider: %s)",
                     cid, info["pid"], duration_min,
                     info.get("provider", "claude"))

            # Local provider crash fallback: if the local agent produced no
            # meaningful output, re-queue the issue to retry with claude.
            if (info.get("provider") == "local"
                    and len(output_text.strip()) < 50
                    and not output_text.strip().startswith("##BLOCKED##")):
                log.warning("Local agent produced no output for %s, re-queuing with claude",
                            cid)
                gh_issue_comment(repo, issue_number,
                    f"**Local agent failed** (empty output) — retrying with Claude.\n\n"
                    f"Agent type: `{info['agent']}`, Provider: `{info.get('provider')}`")
                cleanup_worktree(project_dir, issue_number, info.get("project"))
                # Queue for retry with claude — store as retry with forced provider
                retry_info = {
                    "retry_count": info.get("retry_count", 0),
                    "prior_failure": "local_agent_empty_output",
                    "item_id": info.get("item_id", ""),
                    "column": info.get("column", ""),
                    "title": info.get("title", ""),
                    "body": info.get("body", ""),
                    "issue_repo": repo,
                    "target_project": info.get("project", ""),
                    "parent_issue": info.get("parent_issue"),
                    "force_provider": "claude",
                }
                # pipeline/pipeline_index stay in state["pipeline_state"][cid]
                state.setdefault("retry_queue", {})[cid] = retry_info
                output_file.unlink(missing_ok=True)
                stderr_file.unlink(missing_ok=True)
                del state["running"][cid]
                save_state(state)
                continue

            # Check for BLOCKED marker first (unchanged behavior)
            blocker = None
            for line in output_text.splitlines():
                stripped = line.strip()
                if stripped.startswith("##BLOCKED##"):
                    blocker = stripped.replace("##BLOCKED##", "BLOCKED:", 1)
                    break
                elif stripped.startswith("BLOCKED:"):
                    blocker = stripped
                    break

            if blocker:
                log.info("Issue %s is blocked: %s", cid, blocker)
                gh_issue_comment(repo, issue_number,
                    f"**Agent blocked** — moving to Review.\n\n"
                    f"`{blocker}`\n\n"
                    f"Agent type: `{info['agent']}`")
                move_issue_to_column(fields_cache, info.get("item_id", ""), "Review")
                cleanup_worktree(project_dir, issue_number, info.get("project"))
                _notify_epic_blocked(info, cid, blocker, repo, config)
                output_file.unlink(missing_ok=True)
                stderr_file.unlink(missing_ok=True)
                del state["running"][cid]
                save_state(state)
                continue

            # --- Smart Evaluation ---
            # Build a minimal item dict for the evaluator
            eval_item = {
                "title": info.get("title", ""),
                "body": info.get("body", ""),
            }
            eval_result, eval_reason = evaluate(
                output_text, eval_item, info, config, CLAUDE_BIN
            )
            log.info("Issue %s evaluation: %s — %s", cid, eval_result, eval_reason)

            if eval_result == "fail_retry":
                retry_count = info.get("retry_count", 0)
                max_retries = config.get("evaluation", {}).get("max_retries", 2)
                log.warning("Issue %s failed evaluation (retry %d/%d): %s",
                            cid, retry_count + 1, max_retries, eval_reason)
                gh_issue_comment(repo, issue_number,
                    f"**Evaluation failed** (attempt {retry_count + 1}/{max_retries}): "
                    f"{eval_reason}\n\nRetrying with failure context.")

                # Queue for retry — store failure context, clean worktree, re-dispatch next cycle
                retry_info = {
                    "retry_count": retry_count + 1,
                    "prior_failure": eval_reason,
                    "item_id": info.get("item_id", ""),
                    "column": info.get("column", ""),
                    "title": info.get("title", ""),
                    "body": info.get("body", ""),
                    "issue_repo": repo,
                    "target_project": info.get("project", ""),
                    "parent_issue": info.get("parent_issue"),
                }
                # pipeline/pipeline_index stay in state["pipeline_state"][cid]
                state.setdefault("retry_queue", {})[cid] = retry_info
                cleanup_worktree(project_dir, issue_number, info.get("project"))
                output_file.unlink(missing_ok=True)
                stderr_file.unlink(missing_ok=True)
                del state["running"][cid]
                save_state(state)
                continue

            if eval_result == "fail_stuck":
                log.warning("Issue %s permanently stuck: %s", cid, eval_reason)
                last_output = output_text[-1500:] if output_text else "(no output)"
                gh_issue_comment(repo, issue_number,
                    f"**Agent permanently stuck** — moving to Stuck column.\n\n"
                    f"**Reason:** {eval_reason}\n"
                    f"**Retries exhausted:** {info.get('retry_count', 0)} attempts\n\n"
                    f"Human intervention required. Remove the `[Stuck]` title prefix to "
                    f"allow re-dispatch after addressing the root cause.\n\n"
                    f"<details><summary>Last output</summary>\n\n```\n{last_output}\n```\n</details>")
                # Prefix the title with [Stuck] to prevent _fix_board_orphans from
                # auto-returning it to Ready. A human must remove the prefix to re-dispatch.
                title = info.get("title", "")
                if title and not title.startswith("[Stuck]"):
                    new_title = f"[Stuck] {title}"
                    subprocess.run(
                        ["gh", "issue", "edit", str(issue_number),
                         "--repo", repo, "--title", new_title],
                        capture_output=True, text=True,
                    )
                # Move to Stuck if the column exists; fall back to Review otherwise.
                target_col = "Stuck" if fields_cache["fields"]["Status"]["options"].get("Stuck") else "Review"
                move_issue_to_column(fields_cache, info.get("item_id", ""), target_col)
                cleanup_worktree(project_dir, issue_number, info.get("project"))
                output_file.unlink(missing_ok=True)
                stderr_file.unlink(missing_ok=True)
                del state["running"][cid]
                save_state(state)
                continue

            # --- Evaluation passed: merge and advance ---
            merge_ok, merge_error = merge_worktree(project_dir, info.get("branch", ""))

            if not merge_ok:
                is_transient = merge_error and ("deadlock" in merge_error.lower() or "ORIG_HEAD" in merge_error)
                label = "Transient merge failure (macOS EDEADLK)" if is_transient else "Merge conflict"

                # Track retry count for persistent conflicts. Transient EDEADLK
                # failures don't count (merge_worktree already retries those).
                # Mirrors the skeptic_rejections pattern to prevent infinite loops
                # where _fix_board_orphans moves Review→Ready and we re-dispatch.
                max_merge_retries = config.get("evaluation", {}).get("max_merge_retries", 3)
                merge_retries = state.setdefault("merge_retries", {})
                count = merge_retries.get(cid, 0)
                if not is_transient:
                    count += 1
                    merge_retries[cid] = count

                # At cap: move to Stuck (non_dispatchable) with [Stuck] prefix to
                # break the infinite loop. The Review column dispatches the review
                # agent every cycle, which triggers another merge attempt — without
                # the cap-hit terminating the loop, count grows unbounded. Observed
                # in production: counters reaching 500+ before manual intervention,
                # burning API credits on agents that did nothing useful each cycle.
                if count >= max_merge_retries:
                    target_col = "Stuck" if fields_cache["fields"]["Status"]["options"].get("Stuck") else "Review"
                    title = info.get("title", "")
                    new_title = title
                    # Strip any prior [Review] prefix from a previous attempt and
                    # ensure exactly one [Stuck] prefix.
                    if new_title.startswith("[Review] "):
                        new_title = new_title[len("[Review] "):]
                    if new_title and not new_title.startswith("[Stuck]"):
                        new_title = f"[Stuck] {new_title}"
                    if new_title != title:
                        log.warning("Merge retry cap hit for %s (count=%d/%d) — marking [Stuck] and parking",
                                    cid, count, max_merge_retries)
                        subprocess.run(
                            ["gh", "issue", "edit", str(issue_number),
                             "--repo", repo, "--title", new_title],
                            capture_output=True, text=True,
                        )
                    log.warning("%s for issue %s (attempt %d/%d) — CAP HIT, moving to %s",
                                label, cid, count, max_merge_retries, target_col)
                    gh_issue_comment(repo, issue_number,
                        f"**{label}** (merge attempt {count}/{max_merge_retries}) — **cap reached, moved to {target_col}**.\n\n"
                        f"Branch `{info.get('branch', '')}` preserved for manual merging.\n\n"
                        f"Agent type: `{info['agent']}`\n"
                        f"Error: `{merge_error}`\n\n"
                        f"To re-dispatch: resolve the merge conflict manually, "
                        f"remove the `[Stuck]` title prefix, and move to Ready.")
                    move_issue_to_column(fields_cache, info.get("item_id", ""), target_col)
                else:
                    log.warning("%s for issue %s (attempt %d/%d), moving to Review",
                                label, cid, count, max_merge_retries)
                    gh_issue_comment(repo, issue_number,
                        f"**{label}** (merge attempt {count}/{max_merge_retries}) — moving to Review.\n\n"
                        f"Branch `{info.get('branch', '')}` preserved for manual merging.\n\n"
                        f"Agent type: `{info['agent']}`\n"
                        f"Error: `{merge_error}`")
                    move_issue_to_column(fields_cache, info.get("item_id", ""), "Review")
            else:
                # Merge succeeded — reset the retry counter. If a prior cap was
                # hit and the title was prefixed with [Review], a human has
                # already cleared it to get here; don't touch the title.
                state.get("merge_retries", {}).pop(cid, None)

                # --- Review routing: Review agent decides where work goes ---
                if info.get("column") == "Review":
                    MAX_REVIEW_REJECTIONS = config.get("evaluation", {}).get("max_skeptic_rejections", 3)
                    review_rejections = state.get("skeptic_rejections", {}).get(cid, 0)

                    verdict = extract_verdict(output_text)
                    if verdict:
                        decision = verdict.get("DECISION", "").upper()
                        route = verdict.get("ROUTE", "").strip()
                        reason = verdict.get("REASON", "no reason given")
                        issues_created = verdict.get("ISSUES_CREATED", "none")

                        # Validate route is a real column
                        valid_columns = set(routes["columns"].keys()) | {"Done"}
                        if route not in valid_columns:
                            route = "Stuck"
                            log.warning("Review gave invalid route '%s', falling back to Stuck",
                                        verdict.get("ROUTE", ""))

                        # Guard: Review must not route to itself (infinite loop)
                        if route == "Review":
                            log.warning("Review %s routed to itself — redirecting to Stuck", cid)
                            route = "Stuck"
                            decision = "REJECT"

                        if decision == "APPROVE":
                            next_column = route
                            # Reset rejection counter on approval
                            state.setdefault("skeptic_rejections", {})[cid] = 0
                            log.info("Review APPROVED %s → %s: %s", cid, route, reason)
                        else:
                            review_rejections += 1
                            if review_rejections >= MAX_REVIEW_REJECTIONS:
                                next_column = "Stuck"
                                reason += f" [Review rejection cap reached ({review_rejections}/{MAX_REVIEW_REJECTIONS}) — escalating to Stuck]"
                                log.warning("Review %s hit max rejections (%d) → Stuck", cid, review_rejections)
                            else:
                                next_column = route
                                state.setdefault("skeptic_rejections", {})[cid] = review_rejections
                                log.info("Review REJECTED %s → %s (%d/%d): %s",
                                         cid, route, review_rejections, MAX_REVIEW_REJECTIONS, reason)

                        comment_body = (
                            f"**Review verdict: {decision}** → **{next_column}**\n\n"
                            f"**Reason:** {reason}\n\n"
                        )
                        review_created = create_followup_issues(
                            issues_created, config, fields_cache, cid,
                            source_parent=info.get("parent_issue"),
                        )
                        if review_created:
                            urls_md = "\n".join(f"- {u}" for u in review_created)
                            comment_body += f"**Issues created ({len(review_created)}):**\n{urls_md}\n\n"
                        elif issues_created and issues_created.lower() != "none":
                            comment_body += f"**Issues noted:** {issues_created}\n\n"

                        # Append raw verdict for transparency
                        raw = output_text[-1500:] if len(output_text) > 1500 else output_text
                        comment_body += (
                            f"<details><summary>Full verdict</summary>\n\n"
                            f"```\n{raw}\n```\n</details>"
                        )
                    else:
                        # Review didn't produce a verdict — escalate to Stuck for human inspection
                        next_column = "Stuck"
                        comment_body = (
                            f"**Review** completed but produced no verdict. "
                            f"Moving to **Stuck** for human inspection.\n\n"
                        )
                        log.warning("Review %s: no verdict found in output", cid)
                else:
                    # --- Normal agents: advance through pipeline ---
                    ps = state.get("pipeline_state", {}).get(cid, {})
                    pipeline = ps.get("pipeline")
                    pipeline_idx = ps.get("pipeline_index", 0)
                    if pipeline and pipeline_idx + 1 < len(pipeline):
                        next_column = pipeline[pipeline_idx + 1]
                    else:
                        next_column = routes["columns"].get(info["column"], {}).get("next", "Done")

                    structured = eval_extract_summary(output_text)
                    if structured:
                        comment_body = (
                            f"**{info['agent']}** completed. ✅ Evaluation passed.\n\n"
                            f"**What was done:** {structured.get('DONE', '(see output)')}\n\n"
                            f"**Files changed:** {structured.get('FILES', 'none listed')}\n\n"
                            f"**Commits:** {structured.get('COMMITS', 'none listed')}\n\n"
                        )
                        followup = structured.get("FOLLOWUP", "none")
                        created_urls = create_followup_issues(
                            followup, config, fields_cache, cid,
                            source_parent=info.get("parent_issue"),
                        )
                        if created_urls:
                            urls_md = "\n".join(f"- {u}" for u in created_urls)
                            comment_body += f"**Follow-up issues created ({len(created_urls)}):**\n{urls_md}\n\n"
                        elif followup and followup.lower() not in ("none", "n/a", ""):
                            comment_body += f"**Follow-up noted (not auto-created):** {followup}\n\n"
                        comment_body += f"Moving to **{next_column}**."
                    else:
                        raw = output_text[-2000:] if len(output_text) > 2000 else output_text
                        comment_body = (
                            f"**{info['agent']}** completed. ✅ Evaluation passed.\n\n"
                            f"<details><summary>Agent output</summary>\n\n"
                            f"```\n{raw}\n```\n</details>\n\n"
                            f"Moving to **{next_column}**."
                        )

                gh_issue_comment(repo, issue_number, comment_body)

                if next_column == "Done":
                    move_issue_to_column(fields_cache, info.get("item_id", ""), "Done")
                    gh_issue_close(repo, issue_number)
                else:
                    move_issue_to_column(fields_cache, info.get("item_id", ""), next_column)

                # Update pipeline index if using pipeline routing.
                # Find the target column nearest to (but not before) current position
                # to handle duplicate column names (e.g., Skeptic appears 3x in feature).
                # If Skeptic routes to Ready, preserve pipeline state so we resume correctly.
                ps = state.get("pipeline_state", {}).get(cid, {})
                pipeline = ps.get("pipeline")
                if pipeline:
                    current_idx = ps.get("pipeline_index", 0)
                    if next_column in pipeline:
                        # Find nearest occurrence at or after current position
                        new_idx = None
                        for i in range(len(pipeline)):
                            if pipeline[i] == next_column:
                                if i >= current_idx:
                                    new_idx = i
                                    break
                        # If not found forward, take the last occurrence before current
                        # (Skeptic routing backward is intentional)
                        if new_idx is None:
                            for i in range(len(pipeline) - 1, -1, -1):
                                if pipeline[i] == next_column:
                                    new_idx = i
                                    break
                        if new_idx is not None:
                            state.setdefault("pipeline_state", {})[cid] = {
                                "pipeline": pipeline,
                                "pipeline_index": new_idx,
                            }
                    elif next_column == "Ready":
                        # Review sent to Ready — advance past Review so next cycle
                        # doesn't re-enter the same Review position (infinite loop).
                        next_idx = current_idx
                        for i in range(current_idx + 1, len(pipeline)):
                            if pipeline[i] != "Review":
                                next_idx = i
                                break
                        else:
                            next_idx = current_idx
                        state.setdefault("pipeline_state", {})[cid] = {
                            "pipeline": pipeline,
                            "pipeline_index": next_idx,
                        }

                cleanup_worktree(project_dir, issue_number, info.get("project"))

                # Epic progress tracking
                _notify_epic_progress(info, cid, repo)

            output_file.unlink(missing_ok=True)
            stderr_file.unlink(missing_ok=True)
            del state["running"][cid]
            save_state(state)
        except (subprocess.CalledProcessError, OSError, json.JSONDecodeError) as e:
            # Expected runtime failures: gh/git CLI errors, filesystem issues,
            # bad agent output. Skip this issue and continue — other harvests
            # may still succeed this cycle.
            log.exception("Error harvesting %s (%s) — skipping", cid, type(e).__name__)
            continue
        # Programmer errors (KeyError from malformed state, AttributeError,
        # TypeError, ValueError) intentionally propagate: crashing the cycle
        # and surfacing the bug is better than silently skipping on a
        # corrupted state["running"] entry.


def _notify_epic_blocked(info, issue_num, blocker, repo, config):
    """Notify parent Epic that a sub-issue is blocked."""
    if not info.get("parent_issue"):
        return
    parent_num = info["parent_issue"]["number"]
    parent_repo = info["parent_issue"].get("repo", repo)
    summary = fetch_epic_summary(parent_repo, parent_num)
    progress = f"{summary['completed']}/{summary['total']}" if summary else "?"
    gh_issue_comment(parent_repo, parent_num,
        f"📋 **Sub-issue #{issue_num} blocked** — Epic progress: {progress}.\n\n"
        f"`{blocker}`")
    log.info("Epic #%d: sub-issue #%s blocked, notified", parent_num, issue_num)


def _notify_epic_progress(info, issue_num, repo):
    """Post Epic progress update and auto-close if all sub-issues done."""
    if not info.get("parent_issue"):
        return
    parent_num = info["parent_issue"]["number"]
    parent_repo = info["parent_issue"].get("repo", repo)
    summary = fetch_epic_summary(parent_repo, parent_num)
    if not summary or summary["total"] == 0:
        return
    progress = f"{summary['completed']}/{summary['total']}"
    percent = summary["percent_completed"]
    if summary["completed"] == summary["total"]:
        gh_issue_comment(parent_repo, parent_num,
            f"🎉 **Epic complete** — all {summary['total']} sub-issues resolved.\n\n"
            f"Closing this Epic automatically.")
        gh_issue_close(parent_repo, parent_num)
        log.info("Epic #%d: all sub-issues complete, auto-closed", parent_num)
    else:
        gh_issue_comment(parent_repo, parent_num,
            f"📊 **Progress update** — sub-issue #{issue_num} completed.\n\n"
            f"**{progress}** sub-issues done ({percent}% complete).")
        log.info("Epic #%d: progress %s (%d%%)", parent_num, progress, percent)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def cleanup_orphans(state, config):
    """Remove worktree directories and agent branches not tracked in state."""
    # Build sets for matching active work
    active_worktrees = set()
    for info in state.get("running", {}).values():
        wt = info.get("worktree", "")
        if wt:
            active_worktrees.add(Path(wt).name)

    # Build per-repo set of active issue numbers from canonical_id keys
    # canonical_id format: "owner/repo/number"
    running_issue_numbers_per_repo: dict = {}
    for cid in state.get("running", {}).keys():
        cid_parts = cid.rsplit("/", 1)
        if len(cid_parts) == 2:
            repo_key = cid_parts[0]   # "owner/repo"
            running_issue_numbers_per_repo.setdefault(repo_key, set()).add(cid_parts[1])

    # Clean orphaned worktree directories
    if WORKTREE_DIR.exists():
        for entry in WORKTREE_DIR.iterdir():
            if not entry.is_dir():
                continue
            if entry.name in active_worktrees:
                continue
            log.info("Cleaning orphaned worktree: %s", entry)
            # Determine which project this worktree belongs to
            git_file = entry / ".git"
            project_dir = None
            if git_file.exists():
                try:
                    gitdir_line = git_file.read_text().strip()
                    if gitdir_line.startswith("gitdir:"):
                        gitdir_path = gitdir_line.split(":", 1)[1].strip()
                        repo_git = Path(gitdir_path).parent.parent
                        project_dir = str(repo_git.parent)
                except (OSError, IndexError):
                    pass

            if project_dir and Path(project_dir).exists():
                # Parse worktree dir name: "repo-name-N" or legacy "N"
                parts = entry.name.rsplit("-", 1)
                num = int(parts[-1]) if parts[-1].isdigit() else 0
                repo = parts[0] if len(parts) > 1 and parts[-1].isdigit() else None
                cleanup_worktree(project_dir, num, repo)
            else:
                try:
                    shutil.rmtree(entry)
                    log.info("Removed orphaned directory: %s", entry)
                except OSError as e:
                    log.warning("Failed to remove orphaned directory %s: %s", entry, e)

    # Clean orphaned agent/* branches from all known projects
    projects_root = Path(config["projects_root"])
    if projects_root.exists():
        for child in projects_root.iterdir():
            if not child.is_dir() or not (child / ".git").exists():
                continue

            # Resolve this repo's canonical owner/repo key from its remote URL
            remote_result = subprocess.run(
                ["git", "-C", str(child), "remote", "get-url", "origin"],
                capture_output=True, text=True,
            )
            child_repo_key = ""
            if remote_result.returncode == 0:
                url = remote_result.stdout.strip()
                # Convert https://github.com/owner/repo.git -> owner/repo
                child_repo_key = url.replace("https://github.com/", "").replace(".git", "")

            allowed_nums = running_issue_numbers_per_repo.get(child_repo_key, set())

            result = subprocess.run(
                ["git", "-C", str(child), "branch", "--list", "agent/*"],
                capture_output=True, text=True,
            )
            for line in result.stdout.splitlines():
                branch = line.strip().lstrip("* ")
                parts = branch.split("/")
                if len(parts) == 2 and parts[1].isdigit():
                    if parts[1] not in allowed_nums:
                        log.info("Deleting orphaned branch %s in %s",
                                 branch, child.name)
                        subprocess.run(
                            ["git", "-C", str(child), "branch", "-D", branch],
                            capture_output=True, text=True,
                        )


def _fix_board_orphans(config, fields_cache):
    """Fix board items with no status or lingering in Review/Stuck — move to Ready.

    Handles:
    - OPEN items with no Status → Ready (Epics → Backlog)
    - OPEN items in Review without a [Review] or [Interview] title prefix → Ready
    - OPEN items in Stuck without a [Stuck] title prefix → Ready
    - CLOSED items not in Done → Done

    Runs every cycle to prevent items from falling into limbo.

    Escape hatches:
    - [Review] prefix: human placed this in Review intentionally; leave it alone
    - [Interview] prefix: same — awaiting human input
    - [Stuck] prefix: max retries exhausted; needs human intervention before retry
    """
    owner = config["github_repo"].split("/")[0]
    project_num = config["github_project_number"]
    project_id = fields_cache["project_id"]
    field_id = fields_cache["fields"]["Status"]["id"]
    ready_id = fields_cache["fields"]["Status"]["options"].get("Ready")
    done_id = fields_cache["fields"]["Status"]["options"].get("Done")
    stuck_id = fields_cache["fields"]["Status"]["options"].get("Stuck")

    if not ready_id or not done_id:
        return

    node_fields = """\
                            id
                            content {
                                ... on Issue { number state title }
                            }
                            fieldValues(first: 15) {
                                nodes {
                                    ... on ProjectV2ItemFieldSingleSelectValue {
                                        name
                                        field { ... on ProjectV2SingleSelectField { name } }
                                    }
                                }
                            }"""
    all_nodes = paginate_project_items(owner, project_num, node_fields)
    if not all_nodes:
        # paginate_project_items already logged the warning; nothing to fix.
        return

    fixed = 0
    for node in all_nodes:
        content = node.get("content")
        if not content:
            continue
        issue_state = content.get("state", "OPEN")
        status = None
        for fv in node.get("fieldValues", {}).get("nodes", []):
            field = fv.get("field", {})
            if field.get("name") == "Status" and "name" in fv:
                status = fv["name"]

        target = None
        title = content.get("title", "")
        backlog_id = fields_cache["fields"]["Status"]["options"].get("Backlog")

        if issue_state == "CLOSED" and status != "Done":
            # Closed but not in Done — move to Done regardless of current status
            # (includes the no-status case: ancient closed items that predate
            # the Status field, which would otherwise pile up in the board's
            # "No Status" bucket forever).
            target = done_id
        elif issue_state == "OPEN" and status is None:
            # No status. Epics go to Backlog (they're containers, not dispatchable).
            is_epic = title.startswith("Let's ") or title.startswith("Epic:")
            target = backlog_id if (is_epic and backlog_id) else ready_id
        elif issue_state == "OPEN" and status == "Review":
            # Only move non-interview/review items back to Ready
            if not (title.startswith("[Interview]") or title.startswith("[Review]")):
                target = ready_id
        elif issue_state == "OPEN" and status == "Stuck" and stuck_id:
            # Move non-[Stuck]-prefixed items back to Ready for another attempt.
            # Items with [Stuck] prefix were placed here intentionally (max retries
            # exhausted) and require human intervention before re-dispatch.
            if not title.startswith("[Stuck]"):
                target = ready_id
        # [Interview] issues in Review stay there — they need human input
        # [Stuck] issues with the prefix stay there — they need human intervention

        if target:
            mutation = f'''mutation {{
                updateProjectV2ItemFieldValue(input: {{
                    projectId: "{project_id}"
                    itemId: "{node['id']}"
                    fieldId: "{field_id}"
                    value: {{ singleSelectOptionId: "{target}" }}
                }}) {{ projectV2Item {{ id }} }}
            }}'''
            result = gh_graphql(mutation)
            if result:
                fixed += 1

    if fixed:
        log.info("Board hygiene: fixed %d orphaned items", fixed)


_REQUIRED_CONFIG_KEYS = (
    "github_repo",
    "github_project_number",
    "projects_root",
    "providers",
    "default_provider",
)

# Top-level state keys that must be dicts.  running/pipeline_state/skeptic_rejections
# are guaranteed by migrate_state; merge_retries/retry_queue are lazily created, so
# their absence is a warning, not a fatal error.
_STATE_DICT_KEYS_REQUIRED = ("running", "pipeline_state", "skeptic_rejections")
_STATE_DICT_KEYS_OPTIONAL = ("merge_retries", "retry_queue")


def phase_orphan_guard(config, fields_cache):
    """Enforce 'every story must have a parent Epic' at data-layer level.

    Runs every cycle. Scans the board for OPEN non-Epic issues with no
    parent linkage (orphans) and either:
      - auto-links to the repo's sole OPEN Epic (unambiguous case), or
      - logs an ERROR naming the orphan so the user can triage.

    Catches orphans regardless of creation path:
      - user-created (GitHub UI — no bakery hook fires)
      - agent raw `gh issue create` without addSubIssue
      - bakery's own create_followup_issues when source_parent is missing
        or the addSubIssue mutation failed

    Orphans will NOT dispatch under the Epic-gate in poll_board. Without
    this guard, they'd sit silently on the board.
    """
    owner = config["github_repo"].split("/")[0]
    project_num = config["github_project_number"]

    node_fields = """
                            content {
                                ... on Issue {
                                    number
                                    title
                                    state
                                    repository { name nameWithOwner }
                                    subIssues(first: 1) { totalCount }
                                    parent { number }
                                }
                            }"""
    all_nodes = paginate_project_items(owner, project_num, node_fields)

    # Build per-repo list of OPEN Epics (anything with sub-issues).
    open_epics_by_repo = {}  # repo_full → [issue_number, ...]
    orphans = []             # list of {"repo": owner/name, "number": N, "title": str}
    for node in all_nodes:
        c = node.get("content") or {}
        if not c or c.get("state") != "OPEN":
            continue
        repo_full = (c.get("repository") or {}).get("nameWithOwner")
        num = c.get("number")
        if not repo_full or num is None:
            continue
        is_epic = ((c.get("subIssues") or {}).get("totalCount") or 0) > 0
        if is_epic:
            open_epics_by_repo.setdefault(repo_full, []).append(num)
            continue
        if c.get("parent"):
            continue
        orphans.append({"repo": repo_full, "number": num, "title": c.get("title", "")})

    if not orphans:
        return

    linked = 0
    ambiguous = []
    no_epic = []
    for orphan in orphans:
        repo = orphan["repo"]
        epics = open_epics_by_repo.get(repo, [])
        if len(epics) == 1:
            # Unambiguous — auto-link.
            parent_num = epics[0]
            issue_url = f"https://github.com/{repo}/issues/{orphan['number']}"
            if link_as_sub_issue(repo, parent_num, issue_url):
                log.info("orphan-guard: auto-linked %s#%d → Epic #%d (sole open Epic in %s)",
                         repo, orphan["number"], parent_num, repo)
                linked += 1
            else:
                log.warning("orphan-guard: failed to link %s#%d to Epic #%d",
                            repo, orphan["number"], parent_num)
        elif len(epics) == 0:
            no_epic.append(f"{repo}#{orphan['number']} \"{orphan['title'][:50]}\"")
        else:
            ambiguous.append(f"{repo}#{orphan['number']} \"{orphan['title'][:50]}\"")

    if linked:
        log.info("orphan-guard: auto-linked %d orphan(s) to sole Epic in repo", linked)
    if ambiguous:
        log.warning(
            "orphan-guard: %d orphan(s) need manual Epic linking — multiple Epics in repo: %s",
            len(ambiguous), " | ".join(ambiguous[:10]),
        )
    if no_epic:
        log.error(
            "orphan-guard: %d orphan(s) in repos with NO open Epic — create an Epic first: %s",
            len(no_epic), " | ".join(no_epic[:10]),
        )


def validate_environment():
    """Fail fast if critical dependencies are missing or misconfigured."""
    errors = []

    if not Path(CLAUDE_BIN).exists():
        errors.append(f"Claude binary not found at {CLAUDE_BIN}")

    for cfg_file in (CONFIG_DIR / "dispatcher.json", CONFIG_DIR / "column-routes.json"):
        if not cfg_file.exists():
            errors.append(f"Config file missing: {cfg_file}")

    try:
        config = load_config()
        for key in ("projects_root", "agents_root"):
            path = Path(config.get(key, ""))
            if not path.exists():
                errors.append(f"{key} does not exist: {path}")
        # Schema check: required top-level keys
        for key in _REQUIRED_CONFIG_KEYS:
            if key not in config:
                errors.append(f"dispatcher.json missing required key: '{key}'")
    except (json.JSONDecodeError, KeyError) as e:
        errors.append(f"Invalid dispatcher.json: {e}")

    if not shutil.which("gh"):
        errors.append("GitHub CLI (gh) not found in PATH")

    if errors:
        for err in errors:
            log.error("Startup validation failed: %s", err)
        sys.exit(1)


def validate_state(state):
    """Validate state.json shape after migration.

    Required top-level keys must be dicts; optional keys warn if missing/wrong type.
    Malformed pipeline_state entries (missing pipeline list or pipeline_index int)
    are pruned so the rest of the cycle doesn't trip over them.

    This is a read-and-prune pass only — no new writes beyond what migrate_state did.
    """
    for key in _STATE_DICT_KEYS_REQUIRED:
        if key not in state:
            log.error("State validation: required key '%s' missing after migration — resetting", key)
            state[key] = {}
        elif not isinstance(state[key], dict):
            log.error(
                "State validation: '%s' is %s, expected dict — resetting",
                key, type(state[key]).__name__,
            )
            state[key] = {}

    for key in _STATE_DICT_KEYS_OPTIONAL:
        if key not in state:
            log.debug("State validation: optional key '%s' absent (will be created on demand)", key)
        elif not isinstance(state[key], dict):
            log.error(
                "State validation: '%s' is %s, expected dict — resetting",
                key, type(state[key]).__name__,
            )
            state[key] = {}

    ps = state.get("pipeline_state", {})
    pruned = 0
    for cid in list(ps.keys()):
        entry = ps[cid]
        if not isinstance(entry, dict):
            log.error(
                "State validation: pipeline_state[%s] is %s, not a dict — pruning",
                cid, type(entry).__name__,
            )
            del ps[cid]
            pruned += 1
            continue
        if not isinstance(entry.get("pipeline"), list):
            log.error(
                "State validation: pipeline_state[%s] missing 'pipeline' list — pruning", cid,
            )
            del ps[cid]
            pruned += 1
            continue
        if not isinstance(entry.get("pipeline_index"), int):
            log.error(
                "State validation: pipeline_state[%s] missing 'pipeline_index' int — pruning", cid,
            )
            del ps[cid]
            pruned += 1

    if pruned:
        log.warning("State validation pruned %d malformed pipeline_state entry/entries", pruned)

    return state


def main():
    global DRY_RUN
    parser = argparse.ArgumentParser(description="Half Bakery Dispatcher")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would happen without spawning agents or moving issues")
    args = parser.parse_args()
    DRY_RUN = args.dry_run

    setup_logging()
    log.info("Dispatcher starting%s", " (DRY RUN)" if DRY_RUN else "")

    # Ensure directories exist
    for d in (STATE_DIR, CACHE_DIR, OUTPUT_DIR, WORKTREE_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)

    validate_environment()

    lock_fd = acquire_lock()
    state = None
    try:
        config = load_config()
        routes = load_routes()
        state = load_state()
        state = migrate_state(state)
        state = validate_state(state)

        cleanup_orphans(state, config)

        # Prune old session log entries (keep 14 days)
        try:
            prune_session_log()
        except Exception:
            pass

        fields_cache = get_project_fields(config)
        if not fields_cache:
            log.error("Cannot fetch project fields, aborting")
            return

        # Phase 2: Timeout check
        phase_timeout_check(state, config, fields_cache)
        save_state(state)

        # Phase 3: Harvest completed/crashed agents (before polling, to free slots)
        phase_harvest(state, config, fields_cache, routes)
        save_state(state)

        # Construct a shared budget tracker for phases 4 and 7.
        # Both phases call budget.reserve() before spawning and budget.release()
        # on failure, so the count stays in sync with state["running"] without
        # either phase re-reading the dict independently.
        _budget_cfg = get_budget_profile(config)
        dispatch_budget = BudgetTracker(
            max_concurrent=_budget_cfg["max_concurrent"],
            running_count=len(state["running"]),
        )

        # Phase 4: Process retry queue (re-dispatch failed evaluations)
        phase_retry_queue(state, config, fields_cache, routes, dispatch_budget)
        save_state(state)

        # Phase 4.5: Orphan guard — enforce "every story has a parent Epic"
        # across the whole board. Runs before board-hygiene and poll so any
        # auto-linkage this cycle is visible to downstream phases.
        if not DRY_RUN:
            phase_orphan_guard(config, fields_cache)

        # Phase 5: Board hygiene — fix orphaned items (None/Review → Ready, Closed → Done)
        if not DRY_RUN:
            _fix_board_orphans(config, fields_cache)

        # Phase 6: Discover work when queue is empty
        phase_discover(state, config, fields_cache, dry_run=DRY_RUN)
        save_state(state)

        # Phase 7: Poll and dispatch
        phase_poll_and_dispatch(state, config, fields_cache, routes, dispatch_budget)
        state["last_poll"] = datetime.now(timezone.utc).isoformat()
        save_state(state)

        # Save usage snapshot every cycle
        try:
            save_snapshot()
        except Exception:
            log.debug("Failed to save usage snapshot (non-critical)")

        budget_summary = get_budget_summary(state, config)
        log.info("Dispatcher cycle complete. Running: %d agents. %s",
                 len(state["running"]), budget_summary)
    except Exception:
        log.exception("Dispatcher error")
        if state is not None:
            try:
                save_state(state)
            except Exception:
                log.exception("Failed to save state during error recovery")
    finally:
        release_lock(lock_fd)


if __name__ == "__main__":
    main()
