# The Half Bakery Framework — Project History

## Project Overview

The Half Bakery Framework is a public, sanitized version of Justin's private `half-bakery` build system. It turns GitHub Issues into autonomous Claude agent work sessions: write a ticket, drag it to "Ready," and a Python dispatcher picks it up, routes it to the right specialist agent, runs it through a pipeline, and only advances when the work passes evaluation.

**Core philosophy:** Deterministic dispatch + stateless agents. Python handles all the boring stuff (polling, routing, state, merges, evaluation). Agents receive exactly two things — a persona and an assignment. Zero tokens on coordination overhead.

**Private vs public:** Improvements are dog-fooded on the private `justintormey/half-bakery` system, then sanitized and synced to this public repo. PRs here are typically sync commits from the private system.

---

## Key Context & Decisions

### Architecture: No LLM Coordination Layer
**Decision:** Python scripts handle dispatch, routing, and state. Agents are stateless.

**Rationale:** Most multi-agent frameworks burn tokens on coordination — agents polling queues, reading board state, deciding what to do. That's expensive busywork. Pure Python dispatch is cheaper, faster, and more predictable. The dispatcher never "thinks" — it applies rules deterministically.

### Architecture: GitHub Issues as Task Source
**Decision:** GitHub Issues ARE the spec. The Projects board IS the kanban. No additional task system.

**Rationale:** Adding a separate task DB creates sync problems. The issue body has full history, comments, and context. GitHub Projects v2 provides structured state via GraphQL. One source of truth.

### Architecture: Git Worktrees for Agent Isolation
**Decision:** Each agent run gets its own git worktree under `~/.half-bakery/worktrees/`.

**Rationale:** Concurrent agents can't step on each other. Worktrees are cheap (one checkout, multiple working trees). Universal consensus in the multi-agent ecosystem. Merges are atomic and auditable.

### Architecture: launchd over Docker
**Decision:** macOS launchd timer fires `dispatcher.py` every 5 minutes.

**Rationale:** It's already on your Mac. The dispatcher is stateless and crash-safe. No container overhead. Critical plist requirements: `AbandonProcessGroup=true` (launchd kills children on parent exit otherwise), `USER` env var set (Claude Max OAuth needs it), `~/.local/bin` in PATH (where `claude` binary lives).

### Design: Evaluate Before Advancing (v2.0+)
**Decision:** 6-gate layered checks before advancing any agent output. LLM spot-check only on retries.

**Rationale:** v1.x "dispatch and hope" model let agents hallucinate completion. v2.0 checks actual diffs against actual requirements. Five of six gates cost zero tokens (output exists, summary block present, git diff exists, scope match, tests pass). LLM spot-check only fires on second retry — keeps token costs low while catching real failures.

**Fail-closed rule:** Evaluator infra failures (LLM gate timeout/unavailable) now route to Review, not PASS. Masking infra failures as PASS is how hallucinated completions reach Done.

### Design: Skeptic Agent (v2.0–v2.1)
**Decision (v2.0–v2.1):** Separate Skeptic agent reads actual git diffs, can APPROVE/REJECT/REROUTE.

**Replaced in v2.2:** Skeptic merged into a combined `Review` agent. The multi-gate Skeptic pipeline (multiple Skeptic passes per feature) added latency without proportional quality benefit.

### Design: Pipeline Simplification (v2.2)
**Decision (v2.2.0):** 8 pipeline types → 3. 7 agents → 4. Skeptic/QA combined into Review.

**Pipelines now:**
- `engineering`: Engineering → Review → Done
- `design`: Design → Review → Done
- `docs`: Docs → Review → Done

**Rationale:** Research and Architecture stages added value on complex features but created unnecessary latency for chores, polish, and simple bugs. The new `Review` agent combines code quality (formerly QA) with claim verification (formerly Skeptic).

### Design: Issue Classifier Bias (v2.2.1)
**Decision:** Engineering wins on ties. `docs`/`design` must score ≥2x engineering AND ≥5 absolute to win.

**Rationale:** An engineering agent on a docs issue still produces usable output (it writes the docs). A documentarian agent on an engineering issue produces nothing usable. Asymmetric bias toward the safe default. Title hits weighted 3x (strongest intent signal). Manual override available via `pipeline: engineering` in issue body.

### Design: Merge-Retry → Stuck (v2.2.1)
**Decision:** Merge retry cap routes to `Stuck` (non-dispatchable), not `Review` (dispatchable).

**Rationale:** `Review` is a dispatchable column — routing failed merges there caused the dispatcher to keep spawning review agents against the same conflicted branch indefinitely. `Stuck` is explicitly non-dispatchable; requires human resolution.

### Design: Epic Linkage Enforcement
**Decision:** All auto-created issues must be linked to a parent Epic. Orphan issues rejected.

**Rationale:** Orphan issues (no Epic parent) sit inert because the dispatcher's Epic-gate skips them. Enforcing Epic linkage at creation time prevents silent "lost work" where an agent spawns follow-up issues that never get dispatched.

### Design: Data Lifecycle Audit Rule
**Decision:** Any PR touching persisted data shapes (state.json, config, project fields) must audit all writers, live data compatibility, and migration safety.

**Rationale:** In April 2026, a PR adding `pipeline`/`pipeline_index` fields to `pipeline_state` left 11 legacy counter-only entries unhandled. The reader crashed every cycle; the whole fleet was blocked for hours. This rule codifies the lesson.

### Design: Local Agent Support (v2.1+)
**Decision:** `local_agent.py` provides an OpenAI-compatible harness for running local LLMs as agents.

**Rationale:** Claude Max subscription has usage ceilings. Local models (via Ollama/llama.cpp on the network) can handle low-stakes tasks without burning subscription capacity. The `providers` config block in `dispatcher.json` supports both `claude` and `local` providers with per-agent routing and fallback.

### Design: Vision-Driven Discovery
**Decision:** When the queue is empty, the discoverer reads `project-visions.md` and generates issues for unstarted deliverables.

**Rationale:** The dispatcher shouldn't sit idle. Vision-gap scanning ensures the system proactively progresses toward the owner's stated goals, not just reacts to manually-filed tickets. Note: vision scan uses Claude Sonnet (not zero-token) — the "all discovery is deterministic" claim in early docs was incorrect.

---

## Current Status

🟢 **active** — v2.2.1 released 2026-05-07. System is in daily production use on the private `justintormey/half-bakery` instance. Public framework repo is kept in sync.

---

## Unfinished Work

### Immediate Next Steps
- None currently tracked (check open GitHub Issues for active work)

### Future Enhancements (from project vision doc)
- Periodic sync improvements from private `half-bakery` → public framework repo
- Keep private system running and dog-fooding new features before public release

---

## Important Notes

- **iCloud incompatibility:** The private repo was moved out of iCloud on 2026-04-17 after iCloud-sync throttling deadlocked launchd. Do NOT store the dispatcher or any dev repo in iCloud — it corrupts git refs (`main 2`, `agent/X 2` duplicate files) causing mysterious empty-stderr merge failures.
- **Claude Max subscription model:** The system is designed for a Claude Max subscription, not API keys. Usage budgeting (work hours, concurrency limits, rolling window ceilings) is calibrated for this.
- **Public repo is sanitized:** No PII, internal project names, or private config in this repo. The `config/dispatcher.json` here has placeholder values (`youruser/your-repo`). Actual values live only in the private repo.
- **Review column dual-use (v2.2+):** Before v2.2, `Review` was human-only (dispatcher skipped it). In v2.2, `Review` is both the agent Review column and the human escalation destination for cap-hit failures. Human escalation now goes to `Stuck` instead.

---

## Technical Details

### Key Scripts
| Script | Role |
|--------|------|
| `scripts/dispatcher.py` | Core loop: picks up issues, spawns agents, merges output |
| `scripts/evaluator.py` | 6-gate evaluation + issue classification |
| `scripts/budget.py` | Time-of-day concurrency scheduling |
| `scripts/usage_tracker.py` | Rolling 5h window + weekly token ceiling tracking |
| `scripts/discoverer.py` | Proactive work discovery (TODOs, deps, vulns, vision gaps) |
| `scripts/deployer.py` | Local S3/CloudFront deployment |
| `scripts/local_agent.py` | OpenAI-compatible local LLM harness |

### Agents (v2.2+)
| Agent | Role |
|-------|------|
| `founding-engineer` | Builder — writes code, ships features, fixes bugs |
| `review` | Combined QA + Skeptic — code correctness, security, diff verification |
| `documentarian` | Maintains project history and docs |
| `designer` | Visual/UI design (skips non-UI projects) |

### Version History Summary
| Version | Date | Key Change |
|---------|------|-----------|
| v2.2.1 | 2026-05-07 | Fix merge-retry infinite loop; fix engineering classifier bias |
| v2.2.0 | 2026-04-20 | Pipeline simplification: 8→3 pipelines, 7→4 agents, Skeptic+QA→Review |
| v2.1.1 | 2026-04-20 | Discoverer pagination fix; Skeptic rules hardening |
| v2.1.0 | 2026-04-20 | BudgetTracker class; auto schema migration; fail-closed LLM gate; Designer improvements |
| v2.0.2 | 2026-04-17 | Autonomous follow-up issue creation; Designer agent; reliability fixes |
| v2.0.1 | — | Canonical issue ID refactor |
| v2.0.0 | — | Smart evaluation (6-gate); Skeptic agent; proactive discovery; usage budgeting |

### Config: Critical Fields
```json
{
  "projects_root": "~/PROJECTS",
  "agents_root": "~/PROJECTS/half-bakery/agents",
  "claude_permission_mode": "bypassPermissions",
  "max_concurrent": 3,
  "agent_timeout_minutes": 45
}
```

### launchd Critical Requirements
```xml
<key>AbandonProcessGroup</key><true/>
<!-- USER env var must be set for Claude Max OAuth -->
<!-- PATH must include ~/.local/bin (claude binary) -->
```
