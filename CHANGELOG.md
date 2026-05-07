# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---
## [2.2.1] — 2026-05-07

### Summary

Patch release: two dispatcher bugs that became visible during a long-running
multi-phase project. Both bugs were silent failures — work appeared to "complete"
but burned API credits or routed to the wrong agent type.

---

### 🐛 Bug Fixes

#### Dispatcher: merge-retry cap bypass caused infinite loop (`scripts/dispatcher.py`)

When a worktree's auto-merge failed N times (default `max_merge_retries=3`), the
cap-hit logic prefixed the issue title with `[Review]` and moved the issue to the
`Review` column. But `Review` is a *dispatchable* column — every cycle the
dispatcher spawned a `review` agent against the same conflicted branch, which
re-tried the merge, which failed, which incremented the counter past the cap.
The counter grew unbounded; observed in production reaching the high hundreds
before manual intervention. Each loop iteration cost the price of a `review`
agent run — meaningful budget burn.

**Fix:** at cap, route the issue to `Stuck` (which is in `non_dispatchable`)
with a `[Stuck]` title prefix instead of `[Review]`. The dispatch loop and board
hygiene both skip `[Stuck]`-prefixed Stuck items. Re-dispatch requires a human
to resolve the underlying conflict, remove the prefix, and move the issue back
to `Ready`. The issue comment now spells out this recovery procedure.

This mirrors the existing skeptic-rejection cap behavior, which already routed
to `Stuck` correctly. The merge-retry path was inconsistent.

#### Issue classifier biased toward `docs`/`design` over `engineering` (`scripts/evaluator.py`)

`classify_issue` summed keyword hits and called `max(scores, key=scores.get)`,
which returns the *first* key with the maximum score. Because
`CLASSIFY_KEYWORDS` is iterated in declaration order (`docs, design,
engineering`), any tie between docs/design and engineering went to whichever
came first in the dict. Real-world impact: an engineering issue with a single
incidental "documentation" mention routed to the `documentarian` agent, which
produced zero commits. Multi-phase rollouts where each phase touches a
`history.md` file and references "docs/runbook" paths consistently misclassified.

**Fixes:**
- **Title weighting**: title hits now weighted 3x. Titles are the strongest
  signal of intent; bodies often mention "docs/design" in passing.
- **Engineering bias**: `docs`/`design` only wins when its score is BOTH
  `>= 2x engineering` AND `>= 5` absolute. Otherwise `engineering` wins. The
  asymmetry is deliberate: an engineering agent on a docs issue still produces
  useful output (it'll write the docs); the inverse (documentarian agent on
  an engineering issue) produces nothing usable.
- **Manual override**: a line matching `^pipeline\s*:\s*(engineering|docs|design)$`
  anywhere in the body forces the route, bypassing keyword scoring. Useful for
  edge cases where the issue is genuinely engineering work but the body
  legitimately scores docs-heavy (e.g., adding docs alongside code changes).

#### Backlog status not sticky against UI/automation races

Documented (no code change): when an operator manually sets a project board
status via the GitHub UI, or when GitHub Projects' "auto-add new issues"
automation races a tool's `updateProjectV2ItemFieldValue` mutation, the
intended status can be silently overwritten. The dispatcher's `_fix_board_orphans`
does not move `Backlog` items to `Ready`, so this is not a dispatcher bug —
it's a workflow consideration.

**Recommended pattern** for staged sub-issue rollouts: control gating via the
Epic's status (Epic-gate skips all sub-issues when the parent is in any
non-Ready status), not via individual sub-issue Backlog status. Sub-issue
status can drift independently of the gating decision.

---

### 🔍 Discoverability improvements

- Dispatcher comments at cap-hit now include the recovery procedure (resolve
  conflict, remove `[Stuck]` prefix, move to Ready).
- New cap-hit log line is distinct: `... — CAP HIT, moving to Stuck` so it's
  trivially `grep`-able when scanning for terminal failures.

---

## [2.2.0] — 2026-04-20

### Summary

Major pipeline simplification: Research, Architecture, QA, and Skeptic agents removed. Single Review agent replaces QA + Skeptic. Three pipelines replace eight.

---

### 🔄 Breaking Changes

#### Pipeline refactoring: 8 pipelines → 3, 7 agents → 4
The Research, Architecture, QA, and Skeptic columns and agents have been removed. All work now flows through one of three pipelines:
- `engineering`: Engineering → Review → Done
- `design`: Design → Review → Done
- `docs`: Docs → Review → Done

The new `Review` agent combines QA code review (correctness, security, semver) with Skeptic claim verification (diff audit, hallucination checks, data lifecycle audit, Epic linkage enforcement). Output uses the same `##VERDICT##` block format.

The `column-routes.json` pipeline templates are now keyed `engineering`, `design`, `docs` (replacing `bug`, `feature`, `research`, `architecture`, `chore`, `docs`, `design`, `polish`).

#### Issue classifier keywords tightened
Generic words (`add`, `update`, `new`) removed from the engineering bucket so `design` and `docs` keywords win when present.

---

### 🐛 Bug Fixes

#### Dispatcher: Review column no longer routes to itself on rejection cap
Max-rejection escalation now routes to `Stuck` instead of a human `Review` column (which conflicted with the new agent Review column).

#### FOLLOWUP issue cascade prevention
- Hard cap of 5 followup issues per agent completion (configurable via `max_followup_issues`)
- Title validation rejects entries under 15 chars or starting lowercase (catches research prose bleed-through)
- Sub-issue linkage now fires **before** `project item-add` to prevent GitHub's auto-enroll creating duplicate "Group selected" board entries

#### Epic-gate: parent not on board no longer blocks dispatch
`_ancestors_all_ready` now returns True when a parent exists in GitHub's issue hierarchy but is not on the project board, treating "Epic not board-managed" as "no gate active."

---

## [2.1.1] — 2026-04-20

### Summary

Patch release: discoverer pagination fix and Skeptic AGENTS.md hardening.

---

### 🐛 Bug Fixes

#### discoverer: paginate board queries to handle >100 items
`_scan_vision_gaps` and `_move_issue_to_ready_or_column` now paginate through all project board items using GraphQL `pageInfo`. Previously, boards with >100 items were silently truncated, causing duplicate issue creation and missed Ready-queue items.

#### discoverer: `vision_max_issues_per_scan` config now honored
`_scan_vision_gaps` accepts a `max_issues` parameter (default 15) driven by `vision_max_issues_per_scan` in config. The old hardcoded value of 7 silently ignored the config field.

#### discoverer: module and phase docstrings corrected
Module docstring no longer falsely claims "All discovery is deterministic (zero LLM tokens)" — vision-gap scanning does use Claude Sonnet. `phase_discover` docstring now reflects actual gate behavior (caller decides whether to invoke based on queue depth and agent count).

---

### 🔒 Skeptic Rules Hardened

#### Every Story Has a Parent (Epic Linkage Rule)
Skeptic now **REJECT**s any PR that creates or spawns issues without linking them to a parent Epic. Orphan issues sit inert in the dispatcher's Epic-gate. The review step now includes explicit GraphQL sub-issue linkage verification.

#### Data Lifecycle Audit
Skeptic now **REJECT**s PRs that touch persisted data shapes (state files, project fields, config) without auditing: all other writers, live data compatibility, and migration safety. Precedent: a 2026-04-17 incident blocked the entire fleet for hours due to unaudited schema tightening.

---

## [2.1.0] — 2026-04-20

### Summary

Minor release: schema migration, dispatcher reliability hardening, fail-closed evaluation gate, designer agent improvements, and expanded Skeptic rules. The `BudgetTracker` class replaces ad-hoc concurrency counting; `migrate_state()` makes state schema upgrades safe and automatic. The Skeptic now enforces Epic linkage and data lifecycle audits on every PR. The discoverer no longer fights the Epic-dictates-dispatch model.

---

### ✨ New Features

#### BudgetTracker class
Shared concurrency-slot tracker initialized from `len(state["running"])` so the starting count always reflects already-live agents. Each dispatch phase calls `reserve()` before spawning and `release()` on spawn failure, keeping the tracker in sync with `state["running"]` without either phase re-reading the dict mid-loop. Eliminates the previous pattern of manually managing concurrency counts across dispatch phases.

#### migrate_state() — automatic schema migration
`migrate_state()` runs idempotently on every dispatcher boot, performing two migration passes:

1. **Skeptic rejection counters** — moves `skeptic_rejections` out of `pipeline_state[cid]` objects into the top-level `skeptic_rejections` dict; drops counter-only entries with no `pipeline` key to prevent `KeyError` on the pipeline reader.
2. **Pipeline unification** — moves legacy `pipeline` / `pipeline_index` keys from `state["running"][cid]` into `state["pipeline_state"][cid]` (single authoritative source), without overwriting existing entries.

No manual state wipes required for schema upgrades — the migration handles it.

#### resolve_project_dir: explicit overrides
`resolve_project_dir()` now accepts an optional `overrides` dict (from `config["project_overrides"]`). When a repo name has an override entry, the dispatcher uses the explicit path before attempting any automatic discovery. Useful for repos where the local directory name diverges from the GitHub name or lives outside `projects_root`.

---

### 🐛 Bug Fixes

#### Evaluator: fail-closed LLM gate
Previously, when the LLM spot-check CLI was unavailable (infra failure, timeout, etc.), the gate returned `True` (pass) and the work advanced silently. This allowed unreviewed work to reach Done on infra failure. Fixed: the gate now returns `False` and routes to Review for human inspection. Masking infra failures as PASS is how hallucinated completions sneak through.

#### Discoverer: node_modules exclusion (Python-level post-filter)
The TODO/FIXME scanner relied solely on `grep --exclude-dir` flags to filter non-project directories. BSD grep on macOS can silently ignore `--exclude-dir`, causing false-positive issues from `node_modules/` packages. Fixed with:

- `_EXCLUDED_DIRS` set: canonical list of directories to exclude (`node_modules`, `.git`, `vendor`, `venv`, `dist`, `build`, `__pycache__`, `.next`, `.nuxt`, `coverage`, `.cache`, `.tox`, `target`, `Pods`)
- `_is_excluded_path()` post-filter: drops any grep match whose path contains an excluded directory segment, regardless of grep flag behavior
- Result cap applied **after** filtering — excluded paths no longer consume the 20-result budget before real source matches are counted

#### Discoverer: Backlog auto-promotion disabled
Auto-promotion from Backlog → Ready was fighting the Epic-dictates-dispatch model. The user controls priority explicitly via Epic Status; auto-promotion was bumping Epics from Backlog → Ready behind the user's back. `_promote_from_backlog()` is preserved in code but no longer called from the discovery cycle.

---

### 🤖 Agent Improvements

#### Skeptic: Epic linkage rule
Every issue created or surfaced by an agent must have a parent Epic. Orphan issues (no parent) are ignored by the dispatcher's Epic-gate and sit inert. The Skeptic now **REJECT**s any PR where newly-created issues (via FOLLOWUP, ISSUES_CREATED, or direct `gh issue create`) lack parent Epic linkage.

The Skeptic's issue-creation template now includes the sub-issue GraphQL mutation:
```bash
CHILD_ID=$(gh api /repos/{owner}/{repo}/issues/{new_number} --jq .node_id)
PARENT_ID=$(gh api /repos/{owner}/{repo}/issues/{epic_number} --jq .node_id)
gh api graphql -f query='mutation { addSubIssue(input: { issueId: "'$PARENT_ID'" subIssueId: "'$CHILD_ID'" }) { issue { number } } }'
```

#### Skeptic: Data Lifecycle Audit rule
PRs touching persisted data shapes (state files, GitHub project fields, config files, any dict that survives across runs) now require a three-point audit. Missing any one = **REJECT**:

1. **Other writers** — grep every write site; new fields must be populated by every writer or tolerated by every reader
2. **Live data** — check actual stored values; existing entries must match the new reader's assumptions
3. **Migration** — schema tightening (new required field, renamed key, split schema) demands an idempotent migration

Background: a PR adding `pipeline` / `pipeline_index` to `pipeline_state[cid]` left 11 legacy counter-only entries unhandled. The reader crashed every cycle; the whole fleet was blocked for hours. The audit rule codifies the lesson.

#### Designer: skip if no UI surface
The designer agent now short-circuits with a one-sentence note when the project is pure backend, embedded firmware, CLI-only, or a data pipeline with no web output. Prevents wasted design work on projects where visual design is inapplicable.

#### Designer: brand mapping updates
Updated brand recommendations:
- E-commerce / retail: `airbnb` (was `shopify`)
- Media / editorial: `spotify` (was `wired`)
- Automotive / hardware: `tesla` added

Agent now runs `npx getdesign@latest list` when the project type is ambiguous (brand list grows regularly).

#### Designer: Section 9 (Agent Prompts) instruction
Agent now reads Section 9 of the fetched `DESIGN.md` before applying styles. Section 9 contains the design system author's own AI-specific instructions for applying the file — skipping it was causing agents to miss critical guidance.

---

### 📐 Configuration Changes

#### column-routes.json: Design column
```json
"Design": { "agent": "designer", "next": "Skeptic" }
```
The `designer` agent is now a first-class pipeline column. Routes to Skeptic after completion.

#### column-routes.json: design pipeline
```json
"design": ["Design", "Skeptic", "Done"]
```
New pipeline type for pure design work.

#### column-routes.json: polish pipeline includes Design
```json
"polish": ["Design", "Engineering", "QA", "Skeptic", "Done"]
```
Polish issues now get a design pass before engineering (previously engineering-only).

#### column-routes.json: Stuck added to non_dispatchable
`"Stuck"` added to `non_dispatchable` list. Stuck items (those that have exceeded retry limits or require human intervention) are no longer eligible for auto-dispatch.

---

### 📋 Pipeline Changes

#### Simplified feature pipeline
The `feature` pipeline now has fewer Skeptic gates:

**Before:** `Research → Skeptic → Architecture → Skeptic → Engineering → QA → Docs → Skeptic → Done`

**After:** `Research → Architecture → Engineering → QA → Docs → Skeptic → Done`

The mid-pipeline Skeptic gates after Research and Architecture were adding latency without proportional quality benefit for most feature work. The terminal Skeptic gate before Done remains.

---

### 🔗 Compatibility

All changes are backward-compatible. New config keys are read with `.get()` fallbacks:
- `config.get("project_overrides", {})` — defaults to no overrides
- `evaluation.get("max_skeptic_rejections", 3)` — already in v2.0.2
- `discovery.get("vision_max_issues_per_scan", 15)` — already in v2.0.2

Existing `state.json` files are handled by `migrate_state()` on first boot. No manual wipe required.

[2.1.0]: https://github.com/youruser/The-Half-Bakery-Framework/compare/v2.0.2...v2.1.0

---
## [2.0.2] — 2026-04-17

### Summary

Patch release: autonomous follow-up issue generation, designer agent, six dispatcher/discoverer reliability fixes, and corrected Claude Max 5x usage ceilings. Agents now self-report new work as structured pipe-delimited lines; the dispatcher auto-creates those GitHub issues and adds them to the board. The discoverer's vision scan and orphan rescue are both more reliable at scale.

---

### ✨ New Features

#### Autonomous follow-up issue creation
Agents now emit structured `FOLLOWUP` lines in their `##SUMMARY##` block using a pipe-delimited format: `repo-name | Issue title | Brief description`. The dispatcher parses these after harvest, calls `gh issue create` for each entry, adds the resulting issues to the project board (status: Ready), and reports the created URLs in the completion comment. This closes the loop on agent findings — research agents, engineers, and the Skeptic can all surface new work without human re-entry.

The Skeptic's existing `ISSUES_CREATED` field receives the same treatment.

**Format:**
```
FOLLOWUP: vibecheck-app | Add offline mode | Users reported crashes when offline
          runbook | Automate DNS failover | Identified gap in Chapter 4
```

#### Designer agent
New `designer` specialist agent for visual and 3D design work. Fetches design assets at runtime via `getdesign` and handles layout, component, and visual production tasks. Routed via the `3D Design` column.

---

### 🐛 Bug Fixes

#### Dispatcher: Skeptic infinite loop prevention
Two guards added to prevent the Skeptic from burning unbounded tokens:

1. **Self-routing guard** — if the Skeptic routes to itself (e.g. due to a malformed verdict), the dispatcher intercepts and redirects to Review with a warning.
2. **Max rejections cap** — configurable via `evaluation.max_skeptic_rejections` (default 3). After N rejections, the dispatcher escalates to Review instead of sending work back to the implementation column again.

#### Dispatcher: Ready-first dispatch priority
The dispatch loop now sorts board items so `Ready` items are always processed before mid-pipeline items in the same cycle. Previously, a mid-pipeline item (e.g. Engineering → QA) could starve higher-priority Ready items waiting for their first agent.

#### Dispatcher: macOS EDEADLK merge retry
On macOS, VM resume can cause git merge operations to fail with `Resource deadlock avoided (EDEADLK)`. The dispatcher now detects this transient error and retries the merge once after a 15-second sleep. Without this, in-flight work was silently abandoned (branch cleaned up, issue left stranded).

#### Dispatcher: local directories now mirror GitHub repo names (1:1 convention)
Local project directories are now expected to have the same name as their GitHub counterpart. `vibecheck-app` on GitHub lives in a `vibecheck-app` local directory — no suffix transformation. The dispatcher's `resolve_project_dir()` was updated to match this convention. The `The-Half-Bakery-Framework` repo (which contains the dispatcher infrastructure itself) remains the only known naming exception.

#### Dispatcher: branch names use full worktree ID
Git worktree branches were being named `agent/{issue_number}` (bare integer), which caused branch collisions across repos. Fixed to `agent/{worktree_id}` where `worktree_id` is already scoped as `repo-name-issue-number`.

#### Discoverer: vision scan gets its own quota
The vision scan (which reads `project-visions.md` and generates issues for unstarted deliverables) was gated by `max_per_cycle` — a budget shared with chore/TODO/dependency discovery. A vision scan that ran last produced 1 issue because the chore scanner already consumed the quota. Vision scan now has its own `vision_max_issues_per_scan` limit (default 15) and runs independently of the other discovery channels.

#### Discoverer: orphan rescue pagination
The orphan rescue query fetched at most 100 board items (`items(first: 100)`). Projects with more than 100 items on the board would appear to the discoverer as if those items weren't on the board — causing already-tracked issues to be "rescued" (re-added) on every cycle. Fixed with a full pagination loop using `pageInfo.hasNextPage` / `endCursor`.

#### Discoverer: auto-discovered issues now set Backlog status and link polish epic
Issues created by the discoverer now have their project board status set to `Backlog` and are linked to a polish parent epic when applicable.

---

### 📐 Configuration Changes

New optional config keys:

```json
"evaluation": {
    "max_skeptic_rejections": 3
},
"discovery": {
    "vision_max_issues_per_scan": 15
}
```

---

### 📊 Usage Tracker Corrections

The `usage_tracker.py` 5-hour rolling window ceiling was calibrated for the Claude Max 20x plan, not 5x. Corrected values for Max 5x ($100/month):

| Field | Before | After |
|-------|--------|-------|
| `WINDOW_OUTPUT_CEILING` | 300,000 | 3,500,000 |
| `WINDOW_INPUT_CEILING` | 2,000,000 | 20,000,000 |
| `WEEKLY_OUTPUT_CEILING` | 6,000,000 | 50,000,000 |

The old values caused false PAUSED state — the tracker thought the 5h window was at 85%+ when actual Claude app usage showed ~7%.

[2.0.2]: https://github.com/youruser/The-Half-Bakery-Framework/compare/v2.0.1...v2.0.2

---
## [2.0.1] — 2026-04-17

### Summary

Patch release: Canonical issue ID refactor. All internal state, output filenames, process names, and agent prompts now use globally unique `owner/repo/number` keys (e.g. `justintormey/ledrdr/1`) instead of bare issue numbers. This eliminates cross-repo collisions that caused misdirected GitHub comments and duplicate agent dispatch when two repos shared the same issue number. Dashboard updated to parse canonical IDs. Agent artifacts routed to per-repo `.agent/` directories (gitignored).

---

### 🐛 Bug Fixes

#### Cross-repo issue collisions eliminated
When multiple repos had issues with the same number (e.g. `ledrdr#1` and `CougarCast#1`), the dispatcher used bare numbers as state keys — causing `state["running"]["1"]` collisions, misdirected GitHub comments, and agents dispatched multiple times per cycle.

**Root cause:** `state["running"]`, `state["pipeline_state"]`, `state["retry_queue"]`, output log filenames, and git branch names all used bare `issue_number` integers as keys.

**Fix:** Introduced `canonical_id(issue_repo, issue_number) → "owner/repo/number"` and `safe_id(cid) → "owner-repo-number"` helpers. `poll_board()` stamps `canonical_id` on every board item at the source. All downstream consumers use `canonical_id` as the state key and `safe_id` for filenames and branch names.

#### Duplicate dispatch within a single cycle fixed
A related bug caused the same issue to be dispatched multiple times in one cycle when the `running_issues` dedup set was built once before the loop and not updated mid-loop. Fixed by adding `running_issues.add(item["canonical_id"])` after each dispatch within the loop.

#### `cleanup_orphans` now correctly scopes branch deletion per repo
Previously, `cleanup_orphans` used a flat set of all running issue numbers to gate branch deletion — which could block deletion of `agent/1` in repo A because `repo/B#1` was running. Fixed by building a per-repo `allowed_nums` dict from running canonical IDs.

---

### ✨ Changes

#### Agent artifact directory standardized
Agent prompts now instruct agents to write all output artifacts (QA reports, research notes, engineering docs) to a `.agent/` directory within their target project repo. This directory is gitignored across all repos. Previously, artifacts accumulated in the dispatcher repo root as tracked files.

#### Dashboard updated for canonical IDs
- `dashboard/serve.py`: `/api/output/` endpoint accepts `safe_id` filenames (alphanumeric + dashes) instead of bare integers
- `dashboard/index.html`: Running agent cards parse `canonical_id` keys to display `#issueNumber` and link to the correct repo's GitHub issue page

#### Agent prompts enriched
Both `spawn_agent()` and `spawn_local_agent()` prompts now include:
- `**Issue:** owner/repo/number — title` (canonical reference)
- `**GitHub:** https://github.com/owner/repo/issues/N` (direct link)
- `**Artifacts:** Write to .agent/ in the target project repo`
- Explicit `--repo owner/repo` flag on all `gh issue` commands

---

### 🔗 Compatibility

This is a state-breaking change: any `state.json` with bare integer keys from v2.0.0 will not be recognized as running by v2.0.1. The recommended migration is to stop all in-flight agents (`launchctl stop com.halfbakery.dispatcher`), wipe `~/.half-bakery/state.json`, and restart. In-flight agents will still complete and commit their work; they simply won't be tracked for harvest.

[2.0.1]: https://github.com/youruser/The-Half-Bakery-Framework/compare/v2.0.0...v2.0.1

---
## [2.0.0] — 2026-04-15

### Summary

Major release: Smart Dispatcher v3 with evaluation gates, Skeptic agent, proactive work discovery, usage-aware scheduling, and local deployment module. This is a significant evolution from the v1.x "dispatch and hope" model to a verified, budget-aware, self-improving system.

---

### 🧠 Smart Evaluation (evaluator.py — NEW)
- 6-gate layered evaluation: output exists → summary block → git diff → scope match → test suite → optional LLM spot-check
- Zero tokens for first 5 gates; LLM gate only on retries
- Failed evaluations retry with failure context (max 2), then move to Review
- Column-specific gate configuration (Engineering gets all gates, Research/QA get lighter checks)
- Pipeline classification: issues auto-classified as bug/feature/research/architecture/chore/docs/polish with tailored pipelines

### 🔍 Skeptic Agent (agents/skeptic/ — NEW)
- Verification gate agent that trusts nothing and verifies everything
- Reads actual git diffs, runs tests, compares deliverables against issue requirements
- Can APPROVE (advance), REJECT (send back with feedback), or create new issues for gaps found
- Routes work to any column: Ready, Engineering, Research, Architecture, QA, Done
- Outputs structured ##VERDICT## block parsed by the dispatcher

### 🔎 Proactive Work Discovery (discoverer.py — NEW)
- Scans repos for TODO/FIXME comments, outdated deps, security vulnerabilities, quality gaps
- Vision-driven discovery: reads project-visions.md and generates issues for unstarted deliverables
- Interview questions: creates [Interview] issues for product owner decisions, routed to Review
- Orphan rescue: finds open GitHub issues not on the project board and adds them
- Board hygiene: fixes items with no status, moves closed items to Done
- Backlog → Ready promotion when queue is empty

### 💰 Usage Budgeting (budget.py, usage_tracker.py — NEW)
- Time-of-day scheduling: conservative during work hours, aggressive evenings/weekends
- 5-hour rolling window tracking from per-session token counts
- Weekly ceiling tracking with throttle and pause thresholds
- Per-agent model selection (Opus for complex work, Sonnet for review/docs)
- 429 rate limit detection from debug logs as emergency circuit breaker

### 🚀 Local Deployment (deployer.py — NEW)
- Replaces GitHub Actions deploy workflows with local S3 sync
- `.local/` directory pattern: PII, secrets, and deploy config stay gitignored
- Overlay system: merge local config into clean staging directory before deploy
- CloudFront invalidation after sync
- `deploy-targets.json` configuration for all projects

### 🔄 Pipeline & Routing
- Smart pipeline templates: bugs skip Docs, chores skip QA, features get full chain with Skeptic gates
- Skeptic verdict routing: agents can send work to any column based on review
- Pipeline state preservation: issues resume where they left off after Skeptic rerouting
- Epic/sub-issue support: epics go to Backlog, sub-issues dispatch independently
- Board pagination: handles projects with 100+ board items

### 📋 Agent Improvements
- All agent personas compressed ~50% (caveman-style input reduction)
- "Terse. No filler. Execute before explaining." output style
- Agents instructed to write ALL output in their project's repo, never in half-bakery
- Retry context injection: failed agents get specific feedback on what went wrong

### 🏗️ Infrastructure
- Configurable `--model` flag per agent type
- Local LLM provider with health check and automatic Claude fallback
- `--output-format json` for exact per-session token accounting
- Board hygiene runs every cycle: fixes orphans, closes done items, routes epics to Backlog

### Changed
- `column-routes.json`: Skeptic column added, Research/Architecture route to Skeptic instead of Review
- `dispatcher.json`: budget, evaluation, discovery, agent_models, providers config sections
- Dashboard: dynamic pipeline rendering, usage API endpoint

---


## [1.1.1] — 2026-04-08

### Summary

Documentation patch — remove three deprecated agent personas (marketing-expert, 3d-designer, ceo) from the public framework repo. These were removed from the active dispatch roster in the private working repo but were not cleaned up when v1.1.0 was published.

---

### 📝 Documentation Fixes

#### Stale agent references removed
The following agents were removed from the active roster (they were opinionated personas tied to a specific user's workflow, not general-purpose framework components). All references have been purged:

- **marketing-expert** — `agents/marketing-expert/` directory removed; Marketing column and keywords removed from `column-routes.json`; references removed from README
- **3d-designer** — `agents/3d-designer/` directory removed; 3D Design column and keywords removed from `column-routes.json`; references removed from README
- **ceo** — `agents/ceo/` directory removed; references removed from README (this agent was manual-only with no column)

#### Pipeline diagram corrected
README pipeline diagram now correctly shows:
```
Engineering ──> QA ──> Docs ──> Done     (default pipeline)
Research ──> Ready                        (human reviews, decides next)
Architecture ──> Ready                    (human reviews, decides next)
```
Previously incorrectly showed Research/Architecture routing to "Review" instead of "Ready".

#### Agent count updated
README now correctly states "Five specialists" (founding-engineer, qa, documentarian, research-analyst, architect) rather than "Eight specialists."

---

**Semver rationale:** PATCH — documentation-only correction. No API, behavior, or configuration changes. Removing the agent directories does not break existing deployments (users who had custom agents in these directories would need to keep their local copies, but the framework itself has no dependency on them).


## [1.1.0] — 2026-04-08

### Summary

Six weeks of real-world production use. The dispatch loop is stable and has processed dozens of issues end-to-end. This release captures all the lessons learned: critical launchd bug fixes, new features that emerged from actual usage, and hardening of the dispatcher's resilience.

---

### 🐛 Bug Fixes

#### launchd: Agents crashed silently on every dispatch
All dispatched agents were exiting immediately with zero output. Three independent root causes:

1. **Missing `AbandonProcessGroup`** — By default, launchd kills *all* child processes when the managed script exits. Since the dispatcher spawns agents and then exits, launchd was immediately killing every agent. Fix: `<key>AbandonProcessGroup</key><true/>` in the plist. **This is now documented in the plist template and README.**

2. **Missing `USER` environment variable** — Claude Code's OAuth session (Max subscription auth) uses the `USER` env var to locate credentials. launchd doesn't inherit this from the login session. Without it, every agent exited with "Not logged in." Fix: add `USER` to plist `EnvironmentVariables`.

3. **Missing `~/.local/bin` in PATH** — The `claude` binary lives at `~/.local/bin/claude` but that path wasn't in the launchd plist's `PATH`. Fix: prepend `~/.local/bin` to the plist PATH.

**Additional hardening:** `start_new_session=True` added to `subprocess.Popen` (agents get their own process group as defense-in-depth); `stdin=subprocess.DEVNULL` added (eliminates a 3-second "no stdin" warning from `claude --print`).

#### Cross-repo issue comments went to the wrong repo
When working on issues from repos other than the dispatcher's home repo, `gh_issue_comment` and `gh_issue_close` were using `config["github_repo"]` (always the dispatcher's repo). Comments and closes silently went to the wrong repo. Fix: extract `issue_repo` (from GraphQL `nameWithOwner`) when polling the board, and use it for all GitHub operations on that issue.

#### GraphQL queries hardcoded the repo owner
`get_project_fields()` and `poll_board()` had a hardcoded username in their GraphQL queries. This made the dispatcher fail silently for any user other than the original author. Fix: derive `owner = config["github_repo"].split("/")[0]` dynamically in both functions.

---

### ✨ New Features

#### Auto-derive Target Project
The `Target Project` custom field is no longer required on GitHub issues. The dispatcher now derives the target project from the issue's repository name (`nameWithOwner` from GraphQL), with an exact-match, case-insensitive, and nested-directory fallback chain. Manual `Target Project` field overrides still work.

#### Spanning Projects
Agents working on meta-projects (e.g., a dispatcher or orchestration repo) can now receive `--add-dir` access to all sibling project directories. Configure in `dispatcher.json`:
```json
"spanning_projects": ["your-meta-project"]
```
Agents working on spanning projects receive `--add-dir` flags for every git repo under `projects_root`, giving them cross-portfolio read/write access.

#### Dashboard
A local browser dashboard for monitoring the dispatcher at a glance. Zero external dependencies — Python stdlib HTTP server + vanilla HTML/CSS/JS.
```bash
./dashboard/run
```
Shows running agents, activity feed, project inventory, and pipeline visualization.

#### Epic / Sub-Issue Support
Native support for GitHub's sub-issues feature. Epics (issues with sub-issues attached) are automatically detected and skipped during dispatch — they're containers, not work items. Sub-issues dispatch normally with enriched context:
- Parent Epic title and description are injected into the agent's assignment
- Sibling sub-issue list (number, title, state) is visible to each agent — for context only
- When all sub-issues complete, the parent Epic is auto-closed

Detection is structural: if an issue has sub-issues, it's an Epic. Zero configuration changes needed.

#### Dry-Run Mode
```bash
python3 scripts/dispatcher.py --dry-run
```
Simulates a full dispatch cycle without spawning any agents, posting comments, or moving issues. Useful for validating configuration and testing routing before going live.

#### Startup Validation
`validate_environment()` runs at startup and fails fast if:
- The `claude` binary is not found
- Required config files are missing
- `projects_root` or `agents_root` don't exist
- The `gh` CLI is not in PATH

This surfaces misconfigurations immediately rather than letting them fail silently mid-cycle.

#### Orphan Cleanup
Stale worktree directories and `agent/*` git branches that are no longer tracked in `state.json` are automatically pruned at the start of each dispatcher cycle.

#### GraphQL Retry Logic
All GraphQL calls now retry up to 3 times with a 2-second backoff before failing. Improves resilience against transient GitHub API errors.

#### Structured Agent Output Parsing
Agents that output a `##SUMMARY##...##END##` block get a clean, formatted issue comment with the key fields (what was done, files changed, commits, follow-up needed). Agents without the block fall back to truncated raw output. Format:
```
##SUMMARY##
DONE: <one sentence>
FILES: <comma-separated list>
COMMITS: <SHAs or "none">
FOLLOWUP: <issues to create, or "none">
##END##
```

---

### ⚠️ Configuration Changes

#### `claude_permission_mode`: `acceptEdits` → `bypassPermissions`
Agents are headless (`--print` mode) and cannot respond to permission prompts. Any sandbox prompt causes a hang or silent exit. `bypassPermissions` is now the recommended value. Safety is provided by git worktree isolation and the timeout kill switch, not the sandbox.

#### New config keys
- `spanning_projects` (array, default `[]`) — list of project names whose agents get cross-portfolio `--add-dir` access
- `agent_timeout_minutes` default raised from 30 → 45 to accommodate longer agent sessions

---

### 🔒 Security

- Fixed: GraphQL queries no longer hardcode the repo owner
- Fixed: Issue comments now correctly target the issue's originating repo, not always the dispatcher's home repo
- No secrets, credentials, or PII in the codebase — the `github_repo` config field is user-supplied at setup time

---

### 📁 New Files

- `dashboard/serve.py` — Python stdlib HTTP server
- `dashboard/index.html` — Single-page monitoring UI
- `dashboard/run` — Launcher script

---

## [1.0.0] — 2026-03-31

### Initial release

Core dispatcher: polls GitHub Projects board, auto-routes issues by keyword, spawns Claude CLI agents in isolated git worktrees, harvests output, merges branches, posts issue comments, advances pipeline.

**Components:**
- `scripts/dispatcher.py` — the dispatcher
- `agents/` — 8 agent personas (founding-engineer, qa, documentarian, research-analyst, architect, marketing-expert, 3d-designer, ceo)
- `config/dispatcher.json` + `config/column-routes.json` — pipeline configuration
- `launchd/com.halfbakery.dispatcher.plist` — macOS scheduling

[2.0.0]: https://github.com/youruser/The-Half-Bakery-Framework/compare/v1.1.1...v2.0.0
