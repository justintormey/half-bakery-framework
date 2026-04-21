"""Proactive work discovery for Half Bakery dispatcher.

Scans repos for actionable improvements when the queue is empty.
Creates GitHub issues and adds them to the project board.

Most scans are deterministic (annotation tags, deps, security — zero LLM tokens).
Vision-gap scanning (scan_vision) calls Claude Sonnet to compare
project-visions.md against existing open issues.
"""

import json
import logging
import re
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

log = logging.getLogger("dispatcher.discoverer")

# Build tag keywords dynamically so this file doesn't match its own scanner.
_TAGS = ["TO" + "DO", "FIX" + "ME", "HA" + "CK", "X" + "XX"]
_TAGS_LABEL = "/".join(_TAGS[:2])          # e.g. "TO"+"DO"/"FIX"+"ME"
_TAGS_RE = "|".join(_TAGS)                 # pipe-joined for regex alternation
_GREP_PATTERN = rf"({_TAGS_RE})\b"
_LINE_RE = re.compile(rf'(.+?):(\d+):.*?({_TAGS_RE})[:\s]*(.+)')


def phase_discover(state, config, fields_cache, dry_run=False):
    """Find work proactively. Creates issues in Backlog/Ready.

    Only runs when discovery is enabled in config (discovery.enabled).
    Caller is responsible for any gate on Ready queue depth or running
    agent count before invoking this phase.
    """
    discovery_cfg = config.get("discovery", {})
    if not discovery_cfg.get("enabled", False):
        return

    max_per_cycle = discovery_cfg.get("max_issues_per_cycle", 3)
    cooldown_days = discovery_cfg.get("cooldown_days", 7)
    created_count = 0

    # Load discovery cooldown state
    discoveries = state.setdefault("discoveries", {})
    now = datetime.now(timezone.utc)

    projects_root = Path(config["projects_root"])
    owner = config["github_repo"].split("/")[0]
    project_number = config["github_project_number"]

    for child in sorted(projects_root.iterdir()):
        if created_count >= max_per_cycle:
            break
        if not child.is_dir() or not (child / ".git").exists():
            continue

        # Skip repos with no recent commits (archived/stale)
        last_commit = _get_last_commit_date(child)
        if last_commit and (now - last_commit).days > 30:
            continue

        repo_name = child.name

        # --- Annotation tag scan ---
        # Batch: one issue per repo with all tags, not one issue per tag
        if discovery_cfg.get("scan_todos", True) and created_count < max_per_cycle:
            todo_key = f"todos:{repo_name}"
            if not _in_cooldown(discoveries, todo_key, cooldown_days, now):
                todos = _scan_todos(child)
                if todos:
                    title = f"[{repo_name}] Address {len(todos)} {_TAGS_LABEL} comments"
                    body = "**Auto-discovered** by Half Bakery dispatcher.\n\n"
                    for todo in todos[:15]:
                        body += f"- `{todo['file']}:{todo['line']}` — **{todo['tag']}**: {todo['text'][:80]}\n"
                    if len(todos) > 15:
                        body += f"\n...and {len(todos) - 15} more.\n"

                    if dry_run:
                        log.info("[DRY RUN] Would create annotation issue: %s", title)
                        discoveries[todo_key] = now.isoformat()
                        created_count += 1
                    else:
                        issue_url = _create_issue(owner, repo_name, title, body,
                                                  ["auto-discovered", "chore"],
                                                  project_number)
                        if issue_url:
                            _move_issue_to_backlog(issue_url, config, fields_cache)
                            _link_to_polish_epic(owner, issue_url, config, fields_cache)
                            discoveries[todo_key] = now.isoformat()
                            created_count += 1
                            log.info("Created annotation issue: %s", title)

        # --- Outdated deps scan ---
        if discovery_cfg.get("scan_deps", True) and created_count < max_per_cycle:
            dep_key = f"deps:{repo_name}"
            if not _in_cooldown(discoveries, dep_key, cooldown_days, now):
                outdated = _scan_outdated_deps(child)
                if outdated:
                    title = f"[{repo_name}] Update {len(outdated)} outdated dependencies"
                    body = (
                        f"**Auto-discovered** by Half Bakery dispatcher.\n\n"
                        f"**Outdated packages:**\n"
                    )
                    for dep in outdated[:10]:
                        body += f"- `{dep['name']}`: {dep['current']} → {dep['latest']}\n"
                    if len(outdated) > 10:
                        body += f"\n...and {len(outdated) - 10} more.\n"

                    if dry_run:
                        log.info("[DRY RUN] Would create deps issue: %s", title)
                        discoveries[dep_key] = now.isoformat()
                        created_count += 1
                    else:
                        issue_url = _create_issue(owner, repo_name, title, body,
                                                  ["auto-discovered", "chore"],
                                                  project_number)
                        if issue_url:
                            _move_issue_to_backlog(issue_url, config, fields_cache)
                            _link_to_polish_epic(owner, issue_url, config, fields_cache)
                            discoveries[dep_key] = now.isoformat()
                            created_count += 1
                            log.info("Created deps issue: %s", title)

        # --- Security scan ---
        if discovery_cfg.get("scan_security", True) and created_count < max_per_cycle:
            sec_key = f"security:{repo_name}"
            if not _in_cooldown(discoveries, sec_key, cooldown_days, now):
                vulns = _scan_security(child)
                if vulns:
                    title = f"[{repo_name}] {len(vulns)} security vulnerabilities found"
                    body = (
                        f"**Auto-discovered** by Half Bakery dispatcher.\n\n"
                        f"**Vulnerabilities:**\n"
                    )
                    for vuln in vulns[:5]:
                        body += f"- **{vuln['severity']}**: `{vuln['package']}` — {vuln['title']}\n"

                    if dry_run:
                        log.info("[DRY RUN] Would create security issue: %s", title)
                        discoveries[sec_key] = now.isoformat()
                        created_count += 1
                    else:
                        issue_url = _create_issue(owner, repo_name, title, body,
                                                  ["auto-discovered", "bug", "security"],
                                                  project_number)
                        if issue_url:
                            _move_issue_to_backlog(issue_url, config, fields_cache)
                            _link_to_polish_epic(owner, issue_url, config, fields_cache)
                            discoveries[sec_key] = now.isoformat()
                            created_count += 1
                            log.info("Created security issue: %s", title)

    # --- Vision-driven discovery: compare vision doc against existing issues ---
    # Runs on its own cadence with its own quota — vision is the primary backlog source.
    # NOT gated by max_per_cycle (which governs low-signal chore scans).
    if discovery_cfg.get("scan_vision", True):
        vision_key = "vision_scan"
        vision_cooldown = discovery_cfg.get("vision_cooldown_days", 1)
        vision_max = discovery_cfg.get("vision_max_issues_per_scan", 15)
        vision_created = 0
        if not _in_cooldown(discoveries, vision_key, vision_cooldown, now):
            vision_issues = _scan_vision_gaps(config, owner, project_number, vision_max)
            for vi in vision_issues:
                if vision_created >= vision_max:
                    break
                vi_key = f"vision:{vi['repo']}:{vi['title'][:40]}"
                if _in_cooldown(discoveries, vi_key, cooldown_days, now):
                    continue
                target_status = vi.get("status", "Ready")
                is_question = target_status == "Review"
                labels = ["auto-discovered", "interview" if is_question else "vision"]

                if dry_run:
                    log.info("[DRY RUN] Would create %s: %s",
                             "interview question" if is_question else "vision issue",
                             vi['title'])
                    discoveries[vi_key] = now.isoformat()
                    created_count += 1
                    vision_created += 1
                else:
                    issue_url = _create_issue(owner, vi['repo'], vi['title'], vi['body'],
                                              labels, project_number)
                    if issue_url:
                        if is_question:
                            _move_issue_to_column(issue_url, config, fields_cache, "Review")
                        else:
                            _move_issue_to_ready(issue_url, config, fields_cache)
                        discoveries[vi_key] = now.isoformat()
                        created_count += 1
                        vision_created += 1
                        log.info("Created %s: %s",
                                 "interview question → Review" if is_question else "vision issue → Ready",
                                 vi['title'])
            discoveries[vision_key] = now.isoformat()

    # --- Rescue orphaned open issues (not on board or no status) ---
    if not dry_run:
        rescued = _rescue_orphan_issues(config, fields_cache)
        if rescued:
            log.info("Rescued %d orphan issues → Ready", rescued)

    # --- Aspirational: gap analysis between docs and code ---
    if discovery_cfg.get("scan_gaps", True) and created_count < max_per_cycle:
        for child in sorted(projects_root.iterdir()):
            if created_count >= max_per_cycle:
                break
            if not child.is_dir() or not (child / ".git").exists():
                continue
            repo_name = child.name
            gap_key = f"gaps:{repo_name}"
            if _in_cooldown(discoveries, gap_key, cooldown_days, now):
                continue

            gaps = _scan_quality_gaps(child)
            if gaps:
                title = f"[{repo_name}] Polish: {len(gaps)} quality improvements"
                body = (
                    "**Auto-discovered** by Half Bakery dispatcher.\n\n"
                    "Quality gaps found — these improvements would bring the project "
                    "to a higher level of polish and professionalism:\n\n"
                )
                for gap in gaps[:10]:
                    body += f"- **{gap['category']}**: {gap['description']}\n"

                if dry_run:
                    log.info("[DRY RUN] Would create quality issue: %s", title)
                    discoveries[gap_key] = now.isoformat()
                    created_count += 1
                else:
                    issue_url = _create_issue(owner, repo_name, title, body,
                                              ["auto-discovered", "chore"],
                                              project_number)
                    if issue_url:
                        _move_issue_to_backlog(issue_url, config, fields_cache)
                        _link_to_polish_epic(owner, issue_url, config, fields_cache)
                        discoveries[gap_key] = now.isoformat()
                        created_count += 1
                        log.info("Created quality issue: %s", title)

    # Auto-promotion from Backlog → Ready is DISABLED (2026-04-19).
    # Under the Epic-dictates-dispatch model, the user controls priority
    # explicitly via Epic Status. Auto-promotion was fighting that intent
    # by bumping Epics from Backlog → Ready behind the user's back.
    # The _promote_from_backlog function below is preserved for reference
    # but no longer called from the cycle.

    if created_count > 0:
        log.info("Discovery phase: created %d issues", created_count)


# ---------------------------------------------------------------------------
# Scanners
# ---------------------------------------------------------------------------

# Directories that are never project-owned source code.  Used as both
# grep --exclude-dir hints AND as a Python-level post-filter (defense-in-depth
# against BSD grep silently ignoring --exclude-dir on certain macOS versions).
_EXCLUDED_DIRS = {
    "node_modules", ".git", "vendor", "venv", ".venv",
    "dist", "build", "__pycache__", ".next", ".nuxt",
    "coverage", ".cache", ".tox", "eggs", ".eggs",
    "target",  # Rust / Maven
    "Pods",    # CocoaPods
}

def _is_excluded_path(file_path: str) -> bool:
    """Return True if the file path contains any excluded directory segment."""
    parts = Path(file_path).parts
    return any(part in _EXCLUDED_DIRS for part in parts)


def _scan_todos(project_dir):
    """Find annotation-tag comments in project source code.

    Uses grep --exclude-dir flags as a first pass, then applies a Python-level
    path filter as defense-in-depth — BSD grep on macOS can silently fail to
    honour --exclude-dir, which caused false-positive issues being created for
    node_modules content (half-bakery#122).
    """
    exclude_dir_args = [f"--exclude-dir={d}" for d in _EXCLUDED_DIRS]
    try:
        result = subprocess.run(
            ["grep", "-rn",
             *exclude_dir_args,
             "--include=*.py", "--include=*.js", "--include=*.ts",
             "--include=*.jsx", "--include=*.tsx", "--include=*.swift",
             "--include=*.go", "--include=*.rs",
             "-E", _GREP_PATTERN],
            capture_output=True, text=True, cwd=str(project_dir), timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []

    todos = []
    for line in result.stdout.splitlines():
        match = _LINE_RE.match(line)
        if not match:
            continue
        file_path = match.group(1)
        # Python-level guard: skip any path that traverses an excluded directory.
        if _is_excluded_path(file_path):
            continue
        todos.append({
            "file": file_path,
            "line": int(match.group(2)),
            "tag": match.group(3),
            "text": match.group(4).strip(),
        })
        if len(todos) >= 20:  # cap at 20 source-only results
            break
    return todos


def _scan_outdated_deps(project_dir):
    """Check for outdated dependencies."""
    outdated = []

    # Python
    req_file = project_dir / "requirements.txt"
    setup_py = project_dir / "setup.py"
    pyproject = project_dir / "pyproject.toml"
    if req_file.exists() or setup_py.exists() or pyproject.exists():
        try:
            result = subprocess.run(
                ["pip", "list", "--outdated", "--format=json"],
                capture_output=True, text=True, cwd=str(project_dir), timeout=60,
            )
            if result.returncode == 0 and result.stdout.strip():
                for pkg in json.loads(result.stdout):
                    outdated.append({
                        "name": pkg["name"],
                        "current": pkg["version"],
                        "latest": pkg["latest_version"],
                    })
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
            pass

    # Node
    pkg_json = project_dir / "package.json"
    if pkg_json.exists():
        try:
            result = subprocess.run(
                ["npm", "outdated", "--json"],
                capture_output=True, text=True, cwd=str(project_dir), timeout=60,
            )
            if result.stdout.strip():
                data = json.loads(result.stdout)
                for name, info in data.items():
                    outdated.append({
                        "name": name,
                        "current": info.get("current", "?"),
                        "latest": info.get("latest", "?"),
                    })
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
            pass

    return outdated


def _scan_security(project_dir):
    """Run security audit on project dependencies."""
    vulns = []

    # npm audit
    pkg_json = project_dir / "package.json"
    if pkg_json.exists():
        try:
            result = subprocess.run(
                ["npm", "audit", "--json"],
                capture_output=True, text=True, cwd=str(project_dir), timeout=60,
            )
            if result.stdout.strip():
                data = json.loads(result.stdout)
                for vuln_id, info in data.get("vulnerabilities", {}).items():
                    vulns.append({
                        "package": vuln_id,
                        "severity": info.get("severity", "unknown"),
                        "title": info.get("name", vuln_id),
                    })
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
            pass

    # pip-audit (if available)
    req_file = project_dir / "requirements.txt"
    if req_file.exists():
        try:
            result = subprocess.run(
                ["pip-audit", "--format=json", "-r", str(req_file)],
                capture_output=True, text=True, cwd=str(project_dir), timeout=60,
            )
            if result.returncode == 0 and result.stdout.strip():
                data = json.loads(result.stdout)
                for item in data.get("dependencies", []):
                    for vuln in item.get("vulns", []):
                        vulns.append({
                            "package": item["name"],
                            "severity": vuln.get("fix_versions", ["unknown"])[0] if vuln.get("fix_versions") else "unknown",
                            "title": vuln.get("id", "CVE"),
                        })
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
            pass

    return vulns


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_last_commit_date(project_dir):
    """Get the date of the most recent commit."""
    try:
        result = subprocess.run(
            ["git", "-C", str(project_dir), "log", "-1", "--format=%aI"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return datetime.fromisoformat(result.stdout.strip())
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass
    return None


def _in_cooldown(discoveries, key, cooldown_days, now):
    """Check if a discovery key is still within its cooldown period."""
    last_seen = discoveries.get(key)
    if not last_seen:
        return False
    try:
        last_dt = datetime.fromisoformat(last_seen)
        return (now - last_dt).days < cooldown_days
    except ValueError:
        return False


def _repo_exists_on_github(owner, repo_name, _cache={}):
    """Check if a repo exists on GitHub. Cached per session."""
    key = f"{owner}/{repo_name}"
    if key in _cache:
        return _cache[key]
    try:
        result = subprocess.run(
            ["gh", "repo", "view", key, "--json", "name"],
            capture_output=True, text=True, timeout=10,
        )
        exists = result.returncode == 0
        _cache[key] = exists
        return exists
    except (subprocess.TimeoutExpired, OSError):
        _cache[key] = False
        return False


def _create_issue(owner, repo_name, title, body, labels, project_number):
    """Create a GitHub issue and add it to the project board.

    Creates labels if they don't exist. Skips repos not on GitHub.
    """
    repo_full = f"{owner}/{repo_name}"

    if not _repo_exists_on_github(owner, repo_name):
        log.debug("Repo %s not on GitHub, skipping", repo_full)
        return None

    # Ensure labels exist (create if missing, ignore errors)
    for label in labels:
        subprocess.run(
            ["gh", "label", "create", label,
             "--repo", repo_full,
             "--description", "Auto-created by Half Bakery dispatcher",
             "--color", "C5DEF5"],
            capture_output=True, text=True, timeout=10,
        )

    label_args = []
    for label in labels:
        label_args.extend(["--label", label])

    try:
        result = subprocess.run(
            ["gh", "issue", "create",
             "--repo", repo_full,
             "--title", title,
             "--body", body] + label_args,
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            log.warning("Failed to create issue in %s: %s",
                        repo_full, result.stderr.strip())
            return None

        issue_url = result.stdout.strip()
        if not issue_url:
            return None

        # Add to project board and get the item ID back
        add_result = subprocess.run(
            ["gh", "project", "item-add", str(project_number),
             "--owner", owner, "--url", issue_url],
            capture_output=True, text=True, timeout=30,
        )
        if add_result.returncode == 0:
            log.info("Added issue to project board: %s", issue_url)

        return issue_url
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("Issue creation failed: %s", e)
        return None


def _scan_vision_gaps(config, owner, project_number, max_issues=15):
    """Compare project-visions.md against existing open issues across ALL repos.

    Uses a cheap Claude Sonnet call to:
    1. Identify unstarted deliverables → actionable issues
    2. Generate interview questions for the product owner → flagged for Review

    Returns a list of {repo, title, body, status} dicts.
    status is "Ready" for actionable issues, "Review" for interview questions.

    max_issues controls the cap on returned items (driven by vision_max_issues_per_scan
    in config; defaults to 15). Previously this was a hardcoded 7 which silently
    ignored the config value.
    """
    import shutil

    vision_path = Path(config.get("agents_root", "")).parent / "docs" / "project-visions.md"
    if not vision_path.exists():
        return []

    vision_text = vision_path.read_text()

    # Get ALL open issue titles across all repos on the board to avoid duplicates.
    # Paginate so boards with >100 items are fully scanned.
    existing = set()
    cursor = None
    while True:
        after = f', after: "{cursor}"' if cursor else ""
        board_query = f'''query {{
            user(login: "{owner}") {{
                projectV2(number: {project_number}) {{
                    items(first: 100{after}) {{
                        nodes {{
                            content {{
                                ... on Issue {{ title state }}
                            }}
                        }}
                        pageInfo {{ hasNextPage endCursor }}
                    }}
                }}
            }}
        }}'''
        try:
            result = subprocess.run(
                ["gh", "api", "graphql", "-f", f"query={board_query}"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                break
            data = json.loads(result.stdout)
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
            break

        items_data = data.get("data", {}).get("user", {}).get("projectV2", {}).get("items", {})
        for node in items_data.get("nodes", []):
            content = node.get("content")
            if content and content.get("title"):
                existing.add(content["title"].lower()[:60])

        page_info = items_data.get("pageInfo", {})
        if page_info.get("hasNextPage"):
            cursor = page_info["endCursor"]
        else:
            break

    claude_bin = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")

    prompt = f"""You are the product manager for the Half Bakery. Read the vision document below and compare it against the existing open issues.

OUTPUT TWO TYPES OF ITEMS:

1. ACTIONABLE ISSUES (prefix: ISSUE) — specific unstarted deliverables from the vision that have no corresponding open issue. These should be concrete engineering, research, or architecture tasks.

2. INTERVIEW QUESTIONS (prefix: QUESTION) — things you're unsure about or need the product owner's input on before creating issues. Ambiguities, prioritization calls, missing details, technical choices that need a human decision.

Format (one per line, no other text):
ISSUE|repo-name|Title of the issue|One paragraph describing what needs to be done
QUESTION|repo-name|Question title|The specific question for the product owner

Rules:
- repo-name must match the EXACT GitHub repo name (e.g. vibecheck-app, runbook, half-bakery, recon-radar, CougarCast, true-or-do, ugv_rpi)
- Output {max_issues} ISSUES and 3 QUESTIONS
- Prioritize Tier 1 revenue projects (Runbook/runbook, VibeCheck/vibecheck-app) above all else — fill their backlogs DEEP
- Then Tier 2 portfolio projects
- Be specific and granular: one concrete task per issue, not bundles
- Think about the FULL path to shipping: architecture, implementation, testing, distribution, marketing
- Skip anything similar to these existing issues: {', '.join(list(existing)[:40])}
- The backlog should be DEEP — every deliverable in the vision should become multiple issues

Vision document:
{vision_text[:10000]}"""

    try:
        result = subprocess.run(
            [claude_bin, "--print", "--model", "sonnet", "-p", prompt],
            capture_output=True, text=True, timeout=90,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            return []
    except (subprocess.TimeoutExpired, OSError):
        return []

    issues = []
    for line in result.stdout.strip().splitlines():
        if line.startswith("ISSUE|"):
            parts = line.split("|", 3)
            if len(parts) != 4:
                continue
            _, repo, title, body = parts
            repo = repo.strip()
            title = title.strip()
            if title.lower()[:60] in existing:
                continue
            issues.append({
                "repo": repo,
                "title": title,
                "body": f"**Vision-driven** — auto-generated from project-visions.md.\n\n{body.strip()}",
                "status": "Ready",
            })
        elif line.startswith("QUESTION|"):
            parts = line.split("|", 3)
            if len(parts) != 4:
                continue
            _, repo, title, body = parts
            repo = repo.strip()
            title = f"[Interview] {title.strip()}"
            if title.lower()[:60] in existing:
                continue
            issues.append({
                "repo": repo,
                "title": title,
                "body": (
                    f"**Interview question** — the Half Bakery needs your input before proceeding.\n\n"
                    f"{body.strip()}\n\n"
                    f"---\n"
                    f"*Please respond in a comment. The dispatcher will pick up your answer "
                    f"and create follow-up issues based on your direction.*"
                ),
                "status": "Review",
            })

    return issues[:max_issues]


def _scan_quality_gaps(project_dir):
    """Scan a project for quality/professionalism gaps.

    Checks for missing README, missing tests, missing CI, poor docs,
    no license, large files, etc. Zero tokens — all filesystem checks.
    """
    gaps = []
    repo_name = project_dir.name

    # Missing or empty README
    readme = project_dir / "README.md"
    if not readme.exists():
        gaps.append({"category": "Documentation", "description": "No README.md found"})
    elif readme.stat().st_size < 200:
        gaps.append({"category": "Documentation", "description": "README.md is minimal (<200 bytes) — needs project description, setup, usage"})

    # Missing license
    has_license = any(
        (project_dir / name).exists()
        for name in ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING")
    )
    if not has_license:
        gaps.append({"category": "Legal", "description": "No LICENSE file — blocks open-source readiness"})

    # No tests
    has_tests = any([
        list(project_dir.glob("**/test_*.py")),
        list(project_dir.glob("**/*_test.py")),
        list(project_dir.glob("**/*.test.js")),
        list(project_dir.glob("**/*.test.ts")),
        list(project_dir.glob("**/*.spec.js")),
        list(project_dir.glob("**/tests/")),
        list(project_dir.glob("**/__tests__/")),
    ])
    if not has_tests:
        gaps.append({"category": "Testing", "description": "No test files found — add tests for core functionality"})

    # No .gitignore
    if not (project_dir / ".gitignore").exists():
        gaps.append({"category": "Hygiene", "description": "No .gitignore — risk of committing build artifacts, secrets, node_modules"})

    # Missing history.md (Half Bakery convention)
    if not (project_dir / "history.md").exists():
        gaps.append({"category": "Documentation", "description": "No history.md — add project history and architectural decisions"})

    # Check for .env files committed (security risk)
    try:
        result = subprocess.run(
            ["git", "-C", str(project_dir), "ls-files", "--", "*.env", ".env*"],
            capture_output=True, text=True, timeout=10,
        )
        if result.stdout.strip():
            gaps.append({"category": "Security", "description": f"Tracked .env files: {result.stdout.strip()} — should be gitignored"})
    except (subprocess.TimeoutExpired, OSError):
        pass

    # No CHANGELOG
    has_changelog = any(
        (project_dir / name).exists()
        for name in ("CHANGELOG.md", "CHANGELOG", "CHANGES.md")
    )
    if not has_changelog and (project_dir / "package.json").exists():
        gaps.append({"category": "Documentation", "description": "No CHANGELOG.md — add for version tracking"})

    # Missing type hints / linting config
    if list(project_dir.glob("**/*.py")) and not any(
        (project_dir / name).exists()
        for name in ("pyproject.toml", "setup.cfg", ".flake8", ".pylintrc", "ruff.toml")
    ):
        if not (project_dir / "requirements.txt").exists():
            pass  # Probably not a Python project
        else:
            gaps.append({"category": "Quality", "description": "No Python linting config (pyproject.toml/ruff.toml) — add for consistent code style"})

    return gaps


def _rescue_orphan_issues(config, fields_cache):
    """Find open issues NOT on the project board and add them to Ready.

    This prevents issues from falling through the cracks — whether they
    were created manually, by agents, or by discovery without proper
    board placement.
    """
    owner = config["github_repo"].split("/")[0]
    project_num = config["github_project_number"]

    # Get all items currently on the board — paginated to avoid missing items beyond 100
    on_board = set()
    cursor = None
    while True:
        after = f', after: "{cursor}"' if cursor else ""
        query = f'''query {{
            user(login: "{owner}") {{
                projectV2(number: {project_num}) {{
                    items(first: 100{after}) {{
                        nodes {{
                            content {{
                                ... on Issue {{
                                    number
                                    repository {{ nameWithOwner }}
                                }}
                            }}
                        }}
                        pageInfo {{ hasNextPage endCursor }}
                    }}
                }}
            }}
        }}'''
        try:
            result = subprocess.run(
                ["gh", "api", "graphql", "-f", f"query={query}"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                break
            data = json.loads(result.stdout)
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
            break

        items_data = data.get("data", {}).get("user", {}).get("projectV2", {}).get("items", {})
        for node in items_data.get("nodes", []):
            content = node.get("content")
            if content and content.get("number"):
                repo = (content.get("repository") or {}).get("nameWithOwner", "")
                on_board.add(f"{repo}#{content['number']}")

        page_info = items_data.get("pageInfo", {})
        if page_info.get("hasNextPage"):
            cursor = page_info["endCursor"]
        else:
            break

    if not on_board:
        return 0

    # Get all open issues from repos we manage
    projects_root = Path(config["projects_root"])
    rescued = 0
    max_rescue = 5  # cap per cycle

    for child in sorted(projects_root.iterdir()):
        if rescued >= max_rescue:
            break
        if not child.is_dir() or not (child / ".git").exists():
            continue

        repo_name = child.name
        repo_full = f"{owner}/{repo_name}"

        if not _repo_exists_on_github(owner, repo_name):
            continue

        # List open issues
        try:
            result = subprocess.run(
                ["gh", "issue", "list", "--repo", repo_full,
                 "--state", "open", "--json", "number,title", "--limit", "20"],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode != 0:
                continue
            issues = json.loads(result.stdout)
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
            continue

        for issue in issues:
            if rescued >= max_rescue:
                break
            key = f"{repo_full}#{issue['number']}"
            if key not in on_board:
                url = f"https://github.com/{repo_full}/issues/{issue['number']}"
                add_result = subprocess.run(
                    ["gh", "project", "item-add", str(project_num),
                     "--owner", owner, "--url", url],
                    capture_output=True, text=True, timeout=15,
                )
                if add_result.returncode == 0:
                    _move_issue_to_ready(url, config, fields_cache)
                    log.info("Rescued orphan: %s — %s", key, issue['title'][:50])
                    rescued += 1

    return rescued


def _move_issue_to_backlog(issue_url, config, fields_cache):
    """Move a newly-created issue to the Backlog column."""
    backlog_id = fields_cache["fields"].get("Status", {}).get("options", {}).get("Backlog")
    if not backlog_id:
        return
    _move_issue_to_ready_or_column(issue_url, config, fields_cache, backlog_id)


def _link_to_polish_epic(owner, issue_url, config, fields_cache):
    """Add a newly-created chore/polish issue as a sub-issue of the global polish epic.

    The polish epic is the half-bakery issue titled 'Let's polish every project
    to portfolio quality'. Its number is stored in config or discovered once per
    session and cached.
    """
    polish_epic_number = config.get("polish_epic_number")
    if not polish_epic_number:
        return

    # Get the child issue's database ID from its URL
    parts = issue_url.rstrip("/").split("/")
    if len(parts) < 2:
        return
    child_repo = parts[-3]
    child_number = parts[-1]

    try:
        id_result = subprocess.run(
            ["gh", "api", f"/repos/{owner}/{child_repo}/issues/{child_number}", "--jq", ".id"],
            capture_output=True, text=True, timeout=10,
        )
        if id_result.returncode != 0 or not id_result.stdout.strip():
            return
        child_db_id = int(id_result.stdout.strip())

        subprocess.run(
            ["gh", "api", "--method", "POST",
             f"/repos/{owner}/half-bakery/issues/{polish_epic_number}/sub_issues",
             "-H", "Accept: application/vnd.github.sub-issues-preview+json",
             "-F", f"sub_issue_id={child_db_id}"],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass


def _move_issue_to_column(issue_url, config, fields_cache, column_name):
    """Move a board issue to a specific column by name."""
    option_id = fields_cache["fields"].get("Status", {}).get("options", {}).get(column_name)
    if not option_id:
        return
    # Reuse the Ready mover logic but with a different target
    _move_issue_to_ready_or_column(issue_url, config, fields_cache, option_id)


def _move_issue_to_ready(issue_url, config, fields_cache):
    """Move a newly-added issue to the Ready column."""
    ready_id = fields_cache["fields"].get("Status", {}).get("options", {}).get("Ready")
    if not ready_id:
        return
    _move_issue_to_ready_or_column(issue_url, config, fields_cache, ready_id)


def _move_issue_to_ready_or_column(issue_url, config, fields_cache, target_option_id):
    """Move a board issue to a specific column by option ID.

    Looks up its item_id on the board and sets Status.
    Paginates through ALL board items so boards with >100 items are handled.
    """
    owner = config["github_repo"].split("/")[0]
    project_num = config["github_project_number"]

    # Get the item_id by querying the board for this URL
    # Extract issue number from URL
    parts = issue_url.rstrip("/").split("/")
    if len(parts) < 2:
        return
    issue_number = parts[-1]
    repo_full = "/".join(parts[-4:-2]) if len(parts) >= 4 else ""

    # Query board for this issue — paginate to handle boards with >100 items
    item_id = None
    cursor = None
    while True:
        after = f', after: "{cursor}"' if cursor else ""
        query = f'''query {{
            user(login: "{owner}") {{
                projectV2(number: {project_num}) {{
                    items(first: 100{after}) {{
                        nodes {{
                            id
                            content {{
                                ... on Issue {{
                                    number
                                    repository {{ nameWithOwner }}
                                }}
                            }}
                        }}
                        pageInfo {{ hasNextPage endCursor }}
                    }}
                }}
            }}
        }}'''

        try:
            result = subprocess.run(
                ["gh", "api", "graphql", "-f", f"query={query}"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                return
            data = json.loads(result.stdout)
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
            return

        items_data = data.get("data", {}).get("user", {}).get("projectV2", {}).get("items", {})
        for node in items_data.get("nodes", []):
            content = node.get("content")
            if content and str(content.get("number")) == issue_number:
                item_id = node["id"]
                break

        if item_id:
            break

        page_info = items_data.get("pageInfo", {})
        if page_info.get("hasNextPage"):
            cursor = page_info["endCursor"]
        else:
            break

    if not item_id:
        return

    # Move to target column
    field_id = fields_cache["fields"].get("Status", {}).get("id")
    project_id = fields_cache["project_id"]

    if not field_id or not target_option_id:
        return

    mutation = f'''mutation {{
        updateProjectV2ItemFieldValue(input: {{
            projectId: "{project_id}"
            itemId: "{item_id}"
            fieldId: "{field_id}"
            value: {{ singleSelectOptionId: "{target_option_id}" }}
        }}) {{
            projectV2Item {{ id }}
        }}
    }}'''

    subprocess.run(
        ["gh", "api", "graphql", "-f", f"query={mutation}"],
        capture_output=True, text=True, timeout=15,
    )


def _promote_from_backlog(state, config, fields_cache):
    """Move the highest-priority Backlog item to Ready.

    Priority: human-created > bugs > features > auto-discovered.
    Max 1 promotion per cycle.
    """
    owner = config["github_repo"].split("/")[0]
    project_num = config["github_project_number"]

    # Query backlog items
    query = f'''query {{
        user(login: "{owner}") {{
            projectV2(number: {project_num}) {{
                items(first: 50) {{
                    nodes {{
                        id
                        content {{
                            ... on Issue {{
                                number
                                title
                                labels(first: 10) {{ nodes {{ name }} }}
                                state
                            }}
                        }}
                        fieldValues(first: 15) {{
                            nodes {{
                                ... on ProjectV2ItemFieldSingleSelectValue {{
                                    name
                                    field {{ ... on ProjectV2SingleSelectField {{ name }} }}
                                }}
                            }}
                        }}
                    }}
                }}
            }}
        }}
    }}'''

    try:
        result = subprocess.run(
            ["gh", "api", "graphql", "-f", f"query={query}"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
        return None

    backlog_items = []
    for node in data.get("data", {}).get("user", {}).get("projectV2", {}).get("items", {}).get("nodes", []):
        content = node.get("content")
        if not content or content.get("state") != "OPEN":
            continue

        status = None
        for fv in node.get("fieldValues", {}).get("nodes", []):
            field = fv.get("field", {})
            if field.get("name") == "Status" and "name" in fv:
                status = fv["name"]

        if status != "Backlog":
            continue

        labels = [l["name"] for l in content.get("labels", {}).get("nodes", [])]
        is_auto = "auto-discovered" in labels
        is_bug = "bug" in labels
        is_security = "security" in labels

        # Priority scoring: lower = higher priority
        priority = 50  # default
        if is_security:
            priority = 10
        elif not is_auto:
            priority = 20  # human-created
        elif is_bug:
            priority = 30
        else:
            priority = 40  # auto-discovered non-bug

        backlog_items.append({
            "item_id": node["id"],
            "number": content["number"],
            "title": content["title"],
            "priority": priority,
        })

    if not backlog_items:
        return None

    # Sort by priority, pick the best
    backlog_items.sort(key=lambda x: x["priority"])
    best = backlog_items[0]

    # Move to Ready
    status_field = fields_cache["fields"].get("Status", {})
    field_id = status_field.get("id")
    option_id = status_field.get("options", {}).get("Ready")
    project_id = fields_cache["project_id"]

    if not field_id or not option_id:
        return None

    mutation = f'''mutation {{
        updateProjectV2ItemFieldValue(input: {{
            projectId: "{project_id}"
            itemId: "{best['item_id']}"
            fieldId: "{field_id}"
            value: {{ singleSelectOptionId: "{option_id}" }}
        }}) {{
            projectV2Item {{ id }}
        }}
    }}'''

    try:
        result = subprocess.run(
            ["gh", "api", "graphql", "-f", f"query={mutation}"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            log.info("Promoted Backlog → Ready: #%d %s (priority %d)",
                     best["number"], best["title"], best["priority"])
            return best["number"]
    except (subprocess.TimeoutExpired, OSError):
        pass

    return None
