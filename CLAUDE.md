# The Half Bakery Framework — Claude Code Brief

## Product Vision

Public, sanitized version of Justin's private `half-bakery` build system. Turns GitHub Issues into autonomous Claude agent work sessions: write a ticket, drag it to "Ready," and a Python dispatcher picks it up, routes it to the right specialist agent, and only advances when the work passes evaluation. This is the open-source release intended for the community — no PII, no private paths, no credentials. Improvements are dog-fooded on the private `justintormey/half-bakery` system first, then synced here.

## Tech Stack

- **Dispatch core:** Python 3 (dispatcher.py, evaluator.py, budget.py, discoverer.py)
- **Scheduling:** macOS launchd (5-minute poll intervals)
- **Git isolation:** Native git worktrees (`~/.half-bakery/worktrees/`)
- **Agent execution:** `claude` CLI (Claude Max subscription)
- **API:** GitHub CLI (`gh`), GitHub GraphQL
- **Configuration:** JSON (dispatcher.json, column-routes.json, deploy-targets.json)
- **Testing:** pytest via pyproject.toml

## Project Structure

```
scripts/
  dispatcher.py          # Main dispatch loop, worktree creation, issue routing
  evaluator.py           # Verdict classification, 6-gate output evaluation
  budget.py              # Session cost tracking, time-of-day scheduling
  discoverer.py          # Vision-gap scanning, TODO detection, auto-triage
  local_agent.py         # Local LLM agent provider (Ollama/llama.cpp)
  test_evaluator_budget.py  # Unit tests for evaluator + budget
  test_discoverer_todos.py
  test_dispatcher_worktree.py
agents/
  founding-engineer/     # Builder: code, features, fixes
  qa/                    # Quality gate (now merged into review in v2.2)
  research-analyst/      # Investigation and analysis
  architect/             # System design and RFCs
  documentarian/         # Docs, README, project history
  designer/              # UI/UX design, visual systems
  review/                # Combined QA + skeptic verification (v2.2+)
config/
  dispatcher.json        # Core settings: projects_root, agents_root, max_concurrent
  column-routes.json     # Board column → agent routing map
  deploy-targets.json    # Public repo sync targets
dashboard/
  index.html             # Browser-based monitoring UI
  serve.py               # Dev server
launchd/                 # macOS plist (copy to ~/Library/LaunchAgents/)
docs/                    # Architecture docs, RFCs, design specs
```

## Current Status

- ✅ v2.1.1 released (public repo commit `53e3a76`)
- ✅ 6-gate evaluation before advancing any agent output
- ✅ Skeptic/QA merged into Review agent (v2.2 in private system)
- ✅ Budget tracking, time-of-day scheduling, usage ceilings
- ✅ Vision-driven discovery (auto-creates issues from project-visions.md gaps)
- ✅ Local LLM provider support (local_agent.py)
- ⏳ v2.2 sync pending: simplified 3-pipeline model not yet synced to public repo

## Key Decisions & Architecture Notes

- **No LLM coordination layer.** Python handles dispatch, routing, state. Agents receive exactly two things: a persona (AGENTS.md) and an assignment (issue body). Zero tokens on coordination.
- **GitHub Issues are the spec.** No separate task DB. The Projects board is the kanban. GraphQL for state. One source of truth.
- **Git worktrees for isolation.** Each agent gets its own worktree under `~/.half-bakery/worktrees/`. Concurrent agents can't conflict. Merges are atomic and auditable.
- **launchd critical requirements:** `USER` env var, `~/.local/bin` in PATH, `AbandonProcessGroup=true`. Agents crash silently without these.
- **Fail-closed evaluation.** Evaluator infrastructure failures route to Review, not PASS. Masking infra failures as PASS is how hallucinated completions reach Done.
- **Private-first, sanitized public.** Never push private paths (`~/.half-bakery` → generic), credentials, or Justin's personal config to this repo. Run `scripts/deployer.py` from the private system to sync.
- **v2.2 pipeline simplification (private, not yet public):** 8 pipeline types → 3 (`engineering`, `design`, `docs`). Skeptic/QA merged into `Review`. Fewer pipeline stages = less latency for simple issues.

## Agent Notes

- **This is the PUBLIC repo.** Before making any changes, verify no PII, private paths, or credentials exist. Run a PII scan: `grep -r "justintormey" . --include="*.py" --include="*.json" --include="*.md"`.
- **Source of truth is private.** If you're unsure whether a change belongs here, it probably belongs in `justintormey/half-bakery` first. Sync after dog-fooding.
- **Run tests before and after:** `pytest scripts/` — all tests must pass.
- **Config paths must be generic.** Use `~/.half-bakery` not any private path. Use placeholder repo names not `justintormey/*`.
- **Do NOT push private `deployer.py` logic here.** The public deployer only handles framework sync, not Justin's personal deploy targets.
- **Version bumps:** Follow semver. Breaking changes to the pipeline protocol or AGENTS.md contract = MAJOR. New features = MINOR. Fixes = PATCH.
