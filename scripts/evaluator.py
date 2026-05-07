"""Smart evaluation gates for agent output.

Layered checks: deterministic first (zero tokens), LLM spot-check last resort.
Each gate returns (passed: bool, reason: str).
"""

import logging
import re
import subprocess
from pathlib import Path

log = logging.getLogger("dispatcher.evaluator")


# ---------------------------------------------------------------------------
# Individual gates
# ---------------------------------------------------------------------------

def gate_output_exists(output_text):
    """G1: Agent produced meaningful output (>50 chars)."""
    if len(output_text.strip()) > 50:
        return True, "output exists"
    return False, "agent produced no meaningful output (crash or hang)"


def gate_summary_block(output_text):
    """G2: ##SUMMARY##...##END## block is well-formed with required fields."""
    summary = _extract_summary(output_text)
    if not summary:
        return False, "no ##SUMMARY## block found in output"
    required = {"DONE", "FILES", "COMMITS", "FOLLOWUP"}
    missing = required - set(summary.keys())
    if missing:
        return False, f"summary block missing fields: {', '.join(sorted(missing))}"
    if len(summary.get("DONE", "")) < 5:
        return False, "DONE field is empty or too short"
    return True, "summary block well-formed"


def gate_git_diff(worktree_path, branch_name, project_dir):
    """G3: Agent actually changed files (git diff has content)."""
    result = subprocess.run(
        ["git", "-C", worktree_path, "diff", "--stat", "HEAD~1"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        # Try diffing against main/master
        for base in ("main", "master"):
            result = subprocess.run(
                ["git", "-C", worktree_path, "diff", "--stat", base],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                break

    if result.stdout.strip():
        return True, f"files changed: {result.stdout.strip().splitlines()[-1]}"
    return False, "no files changed in worktree"


def gate_scope_match(output_text, issue_title, issue_body, worktree_path):
    """G4: Changed files/content relate to the issue keywords."""
    # Extract keywords from issue (words > 3 chars, not common stop words)
    stop_words = {
        "that", "this", "with", "from", "have", "been", "will", "should",
        "could", "would", "when", "where", "what", "which", "there", "their",
        "then", "than", "them", "they", "into", "also", "some", "more",
        "about", "after", "before", "other", "make", "like", "just",
        "need", "want", "does", "done", "each", "very", "only",
    }
    text = f"{issue_title} {issue_body}".lower()
    words = set(re.findall(r'[a-z][a-z0-9_.-]+', text))
    keywords = {w for w in words if len(w) > 3 and w not in stop_words}

    if not keywords:
        return True, "no keywords to match (trivial issue)"

    # Get the diff content
    result = subprocess.run(
        ["git", "-C", worktree_path, "diff", "--name-only", "HEAD~1"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        for base in ("main", "master"):
            result = subprocess.run(
                ["git", "-C", worktree_path, "diff", "--name-only", base],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                break

    changed_files = result.stdout.lower()
    # Also check the summary DONE field
    summary = _extract_summary(output_text)
    done_text = (summary.get("DONE", "") if summary else "").lower()

    # Check if any issue keywords appear in changed files or done text
    matches = [kw for kw in keywords if kw in changed_files or kw in done_text]
    if matches:
        return True, f"scope match on: {', '.join(matches[:5])}"

    # Looser check: do changed file paths contain any issue-related terms?
    # e.g., issue about "auth" and files in auth/ directory
    for kw in keywords:
        if kw in changed_files:
            return True, f"file path matches keyword: {kw}"

    return False, f"changed files don't relate to issue keywords: {', '.join(sorted(keywords)[:10])}"


def gate_test_suite(worktree_path, test_command):
    """G5: Project tests pass in the worktree."""
    if not test_command:
        return True, "no test command configured (skipped)"

    try:
        result = subprocess.run(
            test_command, shell=True, capture_output=True, text=True,
            cwd=worktree_path, timeout=120,
        )
        if result.returncode == 0:
            return True, "tests passed"
        # Extract last few lines of test output for diagnosis
        output = (result.stdout + result.stderr).strip()
        last_lines = "\n".join(output.splitlines()[-5:])
        return False, f"tests failed:\n{last_lines}"
    except subprocess.TimeoutExpired:
        return False, "test suite timed out (>120s)"
    except OSError as e:
        return True, f"test command failed to run: {e} (skipped)"


def gate_llm_spot_check(issue_title, issue_body, diff_text, claude_bin):
    """G6: Quick LLM check — does the diff address the issue? ~200 tokens."""
    # Truncate diff to avoid blowing the budget
    if len(diff_text) > 3000:
        diff_text = diff_text[:3000] + "\n... (truncated)"

    prompt = (
        f"Issue: {issue_title}\n"
        f"Body: {issue_body[:500]}\n\n"
        f"Diff:\n{diff_text}\n\n"
        f"Does this diff resolve the issue? Reply ONLY: YES or NO + 1 sentence why."
    )

    try:
        result = subprocess.run(
            [claude_bin, "--print", "-p", prompt],
            capture_output=True, text=True, timeout=30,
            stdin=subprocess.DEVNULL,
        )
        answer = result.stdout.strip().upper()
        if answer.startswith("YES"):
            return True, f"LLM: {result.stdout.strip()}"
        return False, f"LLM: {result.stdout.strip()}"
    except (subprocess.TimeoutExpired, OSError) as e:
        # Fail-closed: masking infra failures as PASS is how unreviewed work
        # reaches Done. If the spot-check CLI is unavailable, route to Review
        # so a human decides whether to bypass or fix the infra.
        log.warning("LLM spot-check failed: %s — failing gate (requires human review)", e)
        return False, f"LLM spot-check unavailable ({type(e).__name__}) — requires human review"


# ---------------------------------------------------------------------------
# Gate configuration per column
# ---------------------------------------------------------------------------

# Which gates apply to which columns
COLUMN_GATES = {
    "Engineering": ["output_exists", "summary_block", "git_diff", "scope_match", "test_suite"],
    "Design":      ["output_exists", "summary_block"],
    "Docs":        ["output_exists", "summary_block"],
    "Review":      ["output_exists"],  # Review has its own ##VERDICT## format
}


# ---------------------------------------------------------------------------
# Main evaluation function
# ---------------------------------------------------------------------------

def evaluate(output_text, item, info, config, claude_bin):
    """Run evaluation gates for an agent's output.

    Returns: (result: str, reason: str)
        result is one of: "pass", "fail_retry", "fail_stuck"
    """
    column = info.get("column", "Engineering")
    gates = COLUMN_GATES.get(column, ["output_exists", "summary_block"])

    worktree_path = info.get("worktree", "")
    branch_name = info.get("branch", "")
    project_dir = str(Path(config["projects_root"]) / info.get("project", ""))

    # Resolve test command
    test_commands = config.get("test_commands", {})
    test_command = test_commands.get(info.get("project"), test_commands.get("default"))

    results = []

    for gate_name in gates:
        if gate_name == "output_exists":
            passed, reason = gate_output_exists(output_text)
        elif gate_name == "summary_block":
            passed, reason = gate_summary_block(output_text)
        elif gate_name == "git_diff":
            passed, reason = gate_git_diff(worktree_path, branch_name, project_dir)
        elif gate_name == "scope_match":
            passed, reason = gate_scope_match(
                output_text,
                item.get("title", ""),
                item.get("body", ""),
                worktree_path,
            )
        elif gate_name == "test_suite":
            passed, reason = gate_test_suite(worktree_path, test_command)
        else:
            continue

        results.append((gate_name, passed, reason))
        log.info("  Gate %s: %s — %s", gate_name, "PASS" if passed else "FAIL", reason)

        if not passed:
            retry_count = info.get("retry_count", 0)
            max_retries = config.get("evaluation", {}).get("max_retries", 2)

            if retry_count < max_retries:
                return "fail_retry", f"{gate_name}: {reason}"
            else:
                return "fail_stuck", f"{gate_name}: {reason} (after {retry_count} retries)"

    # Optional LLM spot-check for Engineering on retries
    eval_config = config.get("evaluation", {})
    if (
        eval_config.get("llm_spot_check")
        and column == "Engineering"
        and info.get("retry_count", 0) > 0
    ):
        diff_result = subprocess.run(
            ["git", "-C", worktree_path, "diff", "main"],
            capture_output=True, text=True,
        )
        passed, reason = gate_llm_spot_check(
            item.get("title", ""),
            item.get("body", ""),
            diff_result.stdout,
            claude_bin,
        )
        log.info("  Gate llm_spot_check: %s — %s", "PASS" if passed else "FAIL", reason)
        if not passed:
            return "fail_stuck", f"llm_spot_check: {reason}"

    return "pass", "all gates passed"


# ---------------------------------------------------------------------------
# Helpers (shared with dispatcher)
# ---------------------------------------------------------------------------

def _extract_summary(output_text):
    """Parse the ##SUMMARY##...##END## block from agent output."""
    in_block = False
    fields = {}
    for line in output_text.splitlines():
        stripped = line.strip()
        if stripped == "##SUMMARY##":
            in_block = True
            continue
        if stripped == "##END##":
            break
        if in_block and ":" in stripped:
            key, _, value = stripped.partition(":")
            fields[key.strip()] = value.strip()
    return fields if fields else None


def extract_verdict(output_text):
    """Parse the ##VERDICT##...##END## block from Skeptic agent output.

    Returns dict with DECISION, ROUTE, REASON, ISSUES_CREATED or None.
    """
    in_block = False
    fields = {}
    for line in output_text.splitlines():
        stripped = line.strip()
        if stripped == "##VERDICT##":
            in_block = True
            continue
        if stripped == "##END##" and in_block:
            break
        if in_block and ":" in stripped:
            key, _, value = stripped.partition(":")
            fields[key.strip()] = value.strip()
    return fields if fields else None


# ---------------------------------------------------------------------------
# Pipeline classification
# ---------------------------------------------------------------------------

PIPELINE_TEMPLATES = {
    "engineering": ["Engineering", "Review", "Done"],
    "design":      ["Design", "Review", "Done"],
    "docs":        ["Docs", "Review", "Done"],
}

CLASSIFY_KEYWORDS = {
    "docs":        ["document", "readme", "changelog", "docs", "documentation"],
    "design":      ["design", "ui", "ux", "visual", "layout", "style", "wireframe", "mockup", "figma", "prototype"],
    "engineering": [
        "bug", "fix", "error", "broken", "crash", "regression",
        "implement", "build", "refactor", "cleanup", "maintenance",
        "research", "investigate", "analyze", "architecture",
    ],
}


def classify_issue(title, body):
    """Classify an issue into a pipeline template. Returns (type, pipeline).

    Honors an explicit `pipeline: engineering|docs|design` line anywhere in
    the body (case-insensitive) as an override before keyword scoring.

    Scoring:
      - Title hits weighted 3x (titles are the strongest signal of intent;
        bodies often mention "docs/design" in passing for engineering work).
      - Engineering bias: docs/design must score >= 2x engineering AND >= 5
        absolute to win. Otherwise engineering wins. Misclassifying engineering
        work as docs/design wastes a documentarian/designer agent on code work
        and produces no commits; the inverse (engineering agent on a docs
        issue) is much cheaper because the agent will still write the docs.
      - On a tie, engineering wins.
    """
    title_lower = (title or "").lower()
    body_lower = (body or "").lower()

    # Explicit override: `pipeline: engineering` line in body wins
    m = re.search(r'^\s*pipeline\s*:\s*(engineering|docs|design)\s*$',
                  body_lower, re.MULTILINE)
    if m:
        kind = m.group(1)
        return kind, PIPELINE_TEMPLATES[kind]

    scores = {}
    for issue_type, keywords in CLASSIFY_KEYWORDS.items():
        title_hits = sum(1 for kw in keywords if re.search(r'\b' + re.escape(kw) + r'\b', title_lower))
        body_hits = sum(1 for kw in keywords if re.search(r'\b' + re.escape(kw) + r'\b', body_lower))
        score = title_hits * 3 + body_hits
        scores[issue_type] = score

    eng = scores.get("engineering", 0)
    docs = scores.get("docs", 0)
    design = scores.get("design", 0)

    # Engineering bias: only route to docs/design if their signal is
    # strongly dominant (>= 2x engineering AND >= 5 absolute hits).
    candidates = []
    for kind, score in (("docs", docs), ("design", design)):
        if score >= max(2 * eng, 5):
            candidates.append((kind, score))

    if candidates:
        # Pick highest-scoring docs/design candidate
        best_kind = max(candidates, key=lambda c: c[1])[0]
        return best_kind, PIPELINE_TEMPLATES[best_kind]

    return "engineering", PIPELINE_TEMPLATES["engineering"]
