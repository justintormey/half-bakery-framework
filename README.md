# The Half Bakery Framework

> ## ⚠️ Retired & not maintained — and it never fully worked
>
> **Status: Retired 2026-05-25.** This repo is public as a learning artifact, not
> a working product. No one is maintaining it; issues and pull requests are
> unlikely to be answered.
>
> In the author's own words at retirement:
> *"Sadly, both the Half Bakery and the Half Bakery Framework are busted. Too
> much hassle for what they're worth."*
>
> It did real work and shipped real features — but it was never reliable, its
> central "evaluate before advancing" guarantee had a hole (work reached `Done`
> with the code stranded, unmerged), and its failure modes were almost all
> *silent*, so it demanded constant supervision.
>
> **👉 Read [KNOWN_ISSUES.md](KNOWN_ISSUES.md) before trusting anything below.**
> It is an honest postmortem of what broke and why. The rest of this README
> describes how the system was *meant* to work — treat it as a design essay, and
> note that parts of it have **drifted from the actual code** (see the callouts
> below and in KNOWN_ISSUES).

**Your ideas are half-baked. Let the agents finish cooking.**

Half Bakery turns GitHub Issues into autonomous [Claude](https://claude.ai) agent work sessions. Write a ticket, drag it to "Ready," and walk away. A Python dispatcher picks it up, routes it to the right specialist agent, runs it through a verification pipeline — engineering, QA, skeptic review, docs — and only advances when the work actually passes evaluation.

No orchestration framework. No token-burning coordination layer. Just a cron job, some git worktrees, and Claude doing the actual work.

```
  You (human)                    The Bakery (agents)
  ┌──────────┐                   ┌──────────────────────────┐
  │ Write an │    drag to        │  dispatcher.py           │
  │  issue   │ ──"Ready"──────> │  (runs every 5 min)      │
  │          │                   │                          │
  └──────────┘                   │  1. Pick up ticket       │
       │                         │  2. Classify & route     │
       │                         │  3. Create worktree      │
       │                         │  4. Spawn Claude agent   │
       │                         │  5. Evaluate output      │
       │                         │  6. Retry or advance     │
       │                         └──────────────────────────┘
       │                                    │
       v                                    v
  ┌──────────────────────────────────────────────────────────┐
  │  Research → Skeptic → Architecture → Skeptic →           │
  │  Engineering → QA → Docs → Skeptic → Done                │
  │     🔬         🤨        🏗️         🤨                      │
  │     🔧         🔍       📝        🤨       ✅               │
  └──────────────────────────────────────────────────────────┘
```

## What's New in v2.1.0

> **⚠️ Stale section.** This "What's New" stops at v2.1.0 but the shipped code is
> at **v2.2.1** (see `CHANGELOG.md`). v2.2 simplified 8 pipelines → 3
> (`engineering` / `design` / `docs`) and merged the `qa` + `skeptic` agents into
> a single `review` agent. The pipeline and agent tables further down describe the
> older, pre-v2.2 world. See [KNOWN_ISSUES.md](KNOWN_ISSUES.md#docs-dont-match-the-code).

- **BudgetTracker class** — shared concurrency-slot tracker keeps dispatch phases in sync without re-reading state mid-loop
- **Automatic state migration** — `migrate_state()` runs on every boot; upgrades `state.json` schema without manual wipes
- **Project path overrides** — explicit `project_overrides` config lets you map GitHub repo names to non-standard local paths
- **Fail-closed LLM gate** — evaluator infra failures now route to Review instead of silently advancing unreviewed work
- **Simplified feature pipeline** — Research → Architecture → Engineering → QA → Docs → Skeptic (fewer mid-pipeline Skeptic gates)
- **Skeptic: Epic linkage rule** — REJECTs any PR that creates issues without linking them to a parent Epic
- **Skeptic: Data Lifecycle Audit** — REJECTs PRs touching persisted data without auditing writers, live data, and migration
- **Designer: skip if no UI surface** — prevents wasted design work on backend/CLI/firmware projects
- **Designer: updated brand mapping** — airbnb for e-commerce, spotify for media, tesla for automotive
- **Design pipeline** — new `design` pipeline type; `polish` issues now include a Design pass
- **Stuck is non-dispatchable** — Stuck items no longer eligible for auto-dispatch
- **Node_modules exclusion fixed** — Python-level post-filter catches BSD grep's silent failure to honor `--exclude-dir`
- **Backlog auto-promotion disabled** — discoverer no longer promotes Epics to Ready behind the user's back

### What's in v2.0.2

- **Autonomous Follow-up Issues** — agents emit structured `FOLLOWUP` lines; the dispatcher auto-creates those GitHub issues and adds them to the board (no human re-entry)
- **Designer Agent** — new visual/3D design specialist for layout, component, and visual production work
- **Skeptic Loop Protection** — self-routing guard + configurable max-rejection cap prevent runaway Skeptic cycles
- **EDEADLK Merge Retry** — macOS VM-resume deadlocks no longer silently abandon in-flight work
- **Ready-First Dispatch** — Ready items always take priority over mid-pipeline items in the same cycle
- **Vision Scan Own Quota** — vision discovery no longer starved by chore/TODO scanner budget
- **Orphan Rescue Pagination** — board queries now paginate past 100 items (no more infinite re-rescue loops)
- **Usage Tracker Calibration** — corrected 5h window ceilings for Claude Max 5x (was tuned for 20x)

### What's in v2.0.0

The major release that went from "dispatch and hope" to "evaluate, verify, and retry."

- **Smart Evaluation** — 6-gate layered checks before advancing work (zero tokens for 5 of 6 gates)
- **Skeptic Agent** — verification gate that trusts nothing, reads actual diffs, can reject and reroute
- **Proactive Discovery** — scans repos for TODOs, outdated deps, security vulns, quality gaps
- **Vision-Driven Planning** — reads a project vision doc and generates issues for unstarted work
- **Usage Budgeting** — time-of-day scheduling, per-session token tracking, rolling window ceilings
- **Local Deployment** — `deployer.py` replaces GitHub Actions for S3 deploys
- **Pipeline Classification** — bugs skip Docs, chores skip QA, features get the full chain
- **Board Pagination** — handles projects with 100+ board items

## Why This Exists

Most multi-agent systems burn tokens on coordination — agents polling queues, reading board state, deciding what to do next. That's expensive busywork.

Half Bakery takes a different approach: **deterministic dispatch, stateless agents.** Python scripts handle all the boring stuff (polling, routing, state tracking, merges, evaluation). Agents receive exactly two things: who they are and what to do. No wasted tokens.

The whole thing runs on a Claude Max subscription. No API keys, no per-token billing, no infrastructure beyond your laptop and a launchd timer.

## Quick Start

### Prerequisites

- macOS (uses launchd for scheduling)
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) with an active Max subscription
- [GitHub CLI](https://cli.github.com/) (`gh`) authenticated
- Python 3.10+

### 1. Clone and configure

```bash
git clone https://github.com/youruser/The-Half-Bakery-Framework.git
cd The-Half-Bakery-Framework

# Create runtime directories
mkdir -p ~/.half-bakery/{output,logs,worktrees,cache,usage}
```

Edit `config/dispatcher.json` — set your GitHub repo, projects root, and agent preferences.

### 2. Set up your GitHub Projects board

Create a [GitHub Projects](https://docs.github.com/en/issues/planning-and-tracking-with-projects) board (v2) with these columns:

| Column | Purpose |
|--------|---------|
| **Backlog** | Raw ideas and epics. Dispatcher ignores. |
| **Ready** | Triaged and ready. Dispatcher auto-routes to the right agent. |
| **Research** | Research Analyst investigates. → Skeptic |
| **Architecture** | Architect designs the approach. → Skeptic |
| **Engineering** | Founding Engineer builds it. → QA |
| **Skeptic** | Skeptic verifies the work. Can approve, reject, or reroute. |
| **QA** | QA agent reviews and tests. → Docs |
| **Docs** | Documentarian updates docs. → Skeptic |
| **Review** | Needs human attention. Dispatcher ignores. |
| **Done** | Complete. Issue gets closed. |

### 3. Test it

```bash
# Validate your setup
python3 scripts/dispatcher.py --dry-run

# Run one cycle manually
python3 scripts/dispatcher.py
```

### 4. Install the timer

```bash
cp launchd/com.halfbakery.dispatcher.plist ~/Library/LaunchAgents/
# Edit the plist — update paths and environment variables
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.halfbakery.dispatcher.plist
```

**Critical plist settings** (agents crash silently without these):
- `AbandonProcessGroup` = true (launchd kills children on parent exit otherwise)
- `USER` env var set (Claude Max OAuth needs it)
- `PATH` includes `~/.local/bin` (where the claude binary lives)

## The Agents

> **⚠️ Reality check.** This table lists **seven** agents, but the shipped
> `agents/` directory contains only **four**: `founding-engineer`, `review`,
> `documentarian`, and `designer`. As of v2.2, `qa` + `skeptic` were merged into
> `review`, and the `research-analyst` + `architect` stages were dropped. The
> seven-agent pipeline below describes the pre-v2.2 design, not the running code.

Seven specialists, each with a persona and clear boundaries:

| Agent | Role | What it does |
|-------|------|-------------|
| **founding-engineer** | Builder | Writes code, ships features, fixes bugs |
| **qa** | Quality gate | Reviews code, checks security, enforces conventions |
| **skeptic** | Verification | Trusts nothing. Reads diffs. Approves, rejects, or reroutes. |
| **documentarian** | Memory | Maintains project history and documentation |
| **research-analyst** | Investigation | Structured analysis, market research, feasibility studies |
| **architect** | Design | System design, RFCs, trade-off analysis |
| **designer** | Visual/3D | Layout, component design, and visual production |

Each agent gets a persona file (`AGENTS.md`), execution checklist (`HEARTBEAT.md`), and an isolated git worktree.

### Autonomous Follow-up Issue Creation

When agents complete work, they report new work discovered in the process using a structured `FOLLOWUP` field:

```
##SUMMARY##
DONE: Researched offline storage options for vibecheck
FOLLOWUP: vibecheck-app | Add offline mode | SQLite-backed queue, ~2 days
          vibecheck-app | Handle sync conflicts | Need merge strategy for concurrent edits
##END##
```

The dispatcher parses these lines, creates the GitHub issues, adds them to the board in Ready state, and posts the URLs in the completion comment. Agents generate the backlog. You stay out of the loop.

### Per-Agent Model Selection

Configure which Claude model each agent uses in `dispatcher.json`:

```json
"agent_models": {
    "founding-engineer": "sonnet",
    "architect": "opus",
    "skeptic": "sonnet",
    "qa": "sonnet",
    "documentarian": "sonnet",
    "research-analyst": "sonnet",
    "designer": "sonnet"
}
```

Use Opus for complex reasoning (architecture, deep debugging). Sonnet for everything else — it's cheaper and faster.

## Pipeline & Evaluation

### Smart Classification

Issues are auto-classified when they enter Ready:

| Type | Pipeline | Trigger keywords |
|------|----------|-----------------|
| bug | Engineering → QA → Skeptic → Done | bug, fix, error, crash |
| feature | Research → Skeptic → Architecture → Skeptic → Engineering → QA → Docs → Skeptic → Done | feature, add, implement, build |
| research | Research → Skeptic → Done | research, investigate, explore |
| chore | Engineering → Skeptic → Done | chore, cleanup, refactor, TODO |
| polish | Engineering → QA → Skeptic → Done | polish, quality, improvements |

### Evaluation Gates

After each agent finishes, the output is evaluated before advancing:

| Gate | Cost | What it checks |
|------|------|----------------|
| Output exists | 0 tokens | Agent produced meaningful output (>50 chars) |
| Summary block | 0 tokens | `##SUMMARY##` block is well-formed |
| Git diff | 0 tokens | Files were actually changed |
| Scope match | 0 tokens | Changed files relate to the issue |
| Test suite | 0 tokens | Tests pass (if configured) |
| LLM spot-check | ~200 tokens | Diff addresses the issue (optional, retries only) |

Failed evaluations retry with failure context (max 2 retries), then move to Review for human attention.

### The Skeptic

The Skeptic is the trust-but-verify layer. It:
- Reads actual git diffs, not just summaries
- Compares deliverables against issue requirements
- Can **APPROVE** (advance), **REJECT** (send back with feedback), or create new issues
- Routes work to any column based on its verdict

Loop protection: the dispatcher caps Skeptic rejections at `max_skeptic_rejections` (default 3) and blocks self-routing. After the cap is hit, work escalates to Review for human inspection.

## Proactive Work Discovery

When the queue is empty, the dispatcher doesn't sit idle:

| Source | What it finds |
|--------|--------------|
| TODO/FIXME scan | Actionable comments in source code |
| Outdated deps | `pip list --outdated` / `npm outdated` |
| Security vulns | `npm audit` / `pip-audit` |
| Quality gaps | Missing README, LICENSE, tests, .gitignore |
| Vision gaps | Unstarted deliverables from your project vision doc |
| Interview questions | Ambiguities that need product owner input → Review column |

## Usage Budgeting

Manages Claude Max subscription consumption:

```json
"budget": {
    "work_hours": {"start": 9, "end": 18},
    "work_days": [0, 1, 2, 3, 4],
    "conservative_max": 1,
    "moderate_max": 2,
    "aggressive_max": 4
}
```

- **Work hours (M-F 9-6)**: Conservative — 1 agent, reserves capacity for your interactive use
- **Shoulder hours**: Moderate — 2 agents
- **Nights + weekends**: Aggressive — up to 4 agents

The usage tracker monitors 5-hour rolling window consumption and weekly ceilings. Throttles or pauses dispatch when approaching limits. Detects 429 rate limit errors as an emergency circuit breaker.

## Local Deployment

`deployer.py` replaces GitHub Actions for S3/CloudFront deploys:

```bash
python3 scripts/deployer.py deploy my-project --dry-run    # preview
python3 scripts/deployer.py deploy my-project              # deploy
python3 scripts/deployer.py status                          # show all targets
```

Uses the `.local/` directory pattern — PII, secrets, and deploy config stay gitignored. An overlay system merges local config into a clean staging directory before sync.

Configure targets in `config/deploy-targets.json`.

## Epic / Sub-Issue Support

Epics (issues with sub-issues) are containers — the dispatcher skips them and dispatches sub-issues individually. Sub-issues get enriched context (parent description + sibling awareness). Epics auto-close when all sub-issues complete.

## File Layout

```
half-bakery-framework/
  agents/                    Agent personas
    founding-engineer/       Builder
    qa/                      Quality gate
    skeptic/                 Verification gate (NEW)
    documentarian/           Documentation
    research-analyst/        Investigation
    architect/               System design
  config/
    column-routes.json       Pipeline routing + templates
    dispatcher.json          Runtime configuration
    deploy-targets.json      S3 deploy targets (NEW)
  scripts/
    dispatcher.py            Core dispatcher with pagination + pipeline
    evaluator.py             Evaluation gates + classification (NEW)
    budget.py                Time-based scheduling (NEW)
    usage_tracker.py         Rolling window token tracking (NEW)
    discoverer.py            Proactive work discovery (NEW)
    deployer.py              Local S3 deployment (NEW)
  dashboard/
    serve.py                 Monitoring API + usage endpoint
    index.html               Single-page UI with dynamic pipelines
    run                      Launcher script
  launchd/
    com.halfbakery.dispatcher.plist

~/.half-bakery/              Runtime state (auto-created)
  state.json                 Running agents + pipeline state
  usage/                     Per-session token logs (NEW)
  worktrees/                 Git worktrees per agent
  output/                    Agent stdout logs
  logs/                      Dispatcher + deploy logs
  cache/                     GitHub Projects field IDs
```

## Managing the Dispatcher

```bash
# Start
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.halfbakery.dispatcher.plist

# Stop
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.halfbakery.dispatcher.plist

# Watch logs
tail -f ~/.half-bakery/logs/dispatcher.log

# Check usage
python3 scripts/usage_tracker.py

# Deploy a project
python3 scripts/deployer.py deploy my-project
```

## Design Decisions

**Deterministic dispatch, not LLM coordination.** Python scripts handle routing, state, and merges. Agents get a persona + assignment. Zero tokens on coordination.

**Git worktrees for isolation.** Universal consensus in the multi-agent ecosystem. Prevents concurrent agents from stepping on each other.

**launchd, not Docker.** It's already on your Mac. The dispatcher is stateless and crash-safe.

**GitHub Issues as the task source.** The issue body IS the spec. The Projects board IS the kanban. No new tools.

**Evaluate before advancing.** The v1.x "dispatch and hope" model let agents hallucinate completion. v2.0 checks actual diffs against actual requirements.

**Sonnet by default.** Opus is expensive. Most agent work (QA, docs, skeptic review) doesn't need it. Use Opus only where reasoning depth matters.

## Contributing

**This project is retired and not maintained** — issues and PRs are unlikely to
be reviewed or merged (see [KNOWN_ISSUES.md](KNOWN_ISSUES.md)). You're welcome to
fork it and take it somewhere new. The system is pure Python stdlib + `gh` CLI,
and the agent personas are just markdown, so it's easy to lift parts out.

## License

MIT
