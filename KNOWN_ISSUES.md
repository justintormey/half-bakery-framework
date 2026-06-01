# Known Issues & Postmortem

> **Status: Retired — 2026-05-25. Not maintained. It never fully worked.**
>
> From the retirement note:
> *"Sadly, both the Half Bakery and the Half Bakery Framework are busted. Too much hassle for what they're worth."*

This document is an honest account of what broke, why, and what's still worth
learning from it. It exists because the README describes how the system was
*supposed* to work, and that's not the same as how it *actually* worked. If
you're thinking about running this or building something like it, read this
first.

---

## TL;DR — read this before you trust anything else in this repo

- **It is not maintained.** No one is fixing bugs or answering issues. The
  dispatcher is no longer running anywhere.
- **It was never reliable.** It worked often enough to be seductive and failed
  often enough to be exhausting. The retirement reason was operational fatigue,
  not loss of interest.
- **Its central promise had a hole.** The whole pitch was *evaluate before
  advancing* so agents couldn't fake completion. Work still reached **Done**
  with the actual code stranded, unmerged, on agent branches. See
  ["The board lied"](#the-headline-failure-the-board-lied) below.
- **It only ever ran on one Mac**, coupled to a single Claude Max subscription,
  scheduled by `launchd`, with a fistful of silent-failure footguns.
- **The docs in this repo drift from the code.** The README advertises 7 agents
  and a v2.1.0 feature list; the shipped code has 4 agents at v2.2.1. See
  ["Docs don't match the code"](#docs-dont-match-the-code).

If you take one thing from this project, take the cautionary tale, not the
architecture.

---

## The headline failure: "the board lied"

On 2026-05-07 a full audit was run across the live board — 446 items across 41
repositories. The finding that named the whole problem:

> **Seven issues marked `Done` were never actually shipped.** The feature code
> was stranded on agent branches that never merged to `main`.

This is the failure that matters most, because it strikes at the system's
reason to exist. The v2.0 redesign was explicitly built to kill "dispatch and
hope" — the v1 behavior where an agent would *say* it finished and the
dispatcher would believe it. v2.0 added a six-gate evaluation pipeline and a
Skeptic agent that "reads actual git diffs, not just summaries."

And work *still* reached `Done` without shipping. The evaluation gates checked
that a diff existed and that tests passed **in the agent's worktree** — but a
spanning agent could commit real work to a sibling repo's `agent/*` branch that
then never got merged into that repo's default branch. The board status said
`Done`. GitHub's `main` said otherwise. The gate that was supposed to prevent
exactly this measured the wrong thing.

The audit recovered the stranded work (7 vibecheck issues: #176, #177, #179,
#180, #181, #183, #184) by manually fast-forwarding and cherry-picking, and the
code was patched to scan sibling repos for matching branches and merge them
(`merge_sibling_branches()`). But "we built a verification system and it still
let unshipped work reach Done, and we only caught it with a 5-hour manual audit"
is the kind of finding that erodes trust in the whole apparatus.

---

## Catalog of concrete bugs

These are real, observed failures — most from the 2026-05-07 audit. Many were
fixed. The system was retired anyway, because the *rate* of new failure modes
outpaced the appetite to chase them.

| # | Bug | What actually happened | Disposition |
|---|-----|------------------------|-------------|
| A | **Runaway merge-retry counter** | When a worktree auto-merge failed repeatedly, the cap-hit logic moved the issue to `Review` — a *dispatchable* column. Every cycle re-spawned a review agent, which re-tried the merge, which failed, which incremented the counter past its cap. Observed reaching the **high hundreds** (e.g. `581/3`) before a human noticed. Each loop iteration cost a full agent run. | Fixed (route to non-dispatchable `Stuck` instead). |
| B | **Rate-limit misclassified as failure** | When the Claude CLI returned `You've hit your limit · resets X`, the dispatcher read it as malformed agent output, consumed the retry budget, and parked the issue as `Stuck`. This was the root cause of **7 falsely-stuck issues**. | Fixed (detect rate-limit text as transient; requeue without a retry penalty). |
| C | **Review agent commits its own rejection notes** | The review/skeptic agent committed meaningless `"REJECT pass N"` notes to its own branch as if they were deliverables. Concrete evidence: `agent/vibecheck-app-12 d8b98ff "review: REJECT pass 17 — seventeenth null delivery"`. Seventeen passes, zero delivery. | Fixed later via persona/spawn changes; symptomatic of the review agent having write access it shouldn't. |
| D | **Repo left on a non-main branch with uncommitted changes** | A target repo was left checked out on a feature branch with dirty working state, so the next cycle started from a broken baseline. | Resolved per-incident; systemic cause was Bug G. |
| E | **Prunable/leaked git worktrees** | Worktrees accumulated under `~/.half-bakery/worktrees/` and had to be pruned manually. Root cause never fully understood. | Resolved by `git worktree prune`; cause unclear. |
| F | **An issue that was physically impossible to complete** | A "build the marketing landing page" issue was filed against a macOS-app-only repo with nowhere for a web page to live. The agent could never satisfy it, so it churned. | Resolved by hand (split into a new repo). |
| G | **Spanning agents strand work on sibling branches** | Agents working across repos committed to `agent/*` branches in sibling repos that never merged to those repos' default branches. **This is the mechanism behind "the board lied."** | Fixed in code (sibling-branch scan + merge; conflicts route to `Review`). |
| H | **Diverged git histories (no common ancestor)** | The `vibecheck` repo's local `main` and `origin/main` had **no common ancestor** — the same logical first commit existed under two different SHAs. A textbook iCloud-sync corruption pattern. | Fixed by force-push with a backup tag; only safe because it was a single-collaborator repo. |
| I | **Cross-repo `+N/-0` drift across 41 repos** | The dispatcher committed work locally but didn't reliably push, leaving unpushed commits scattered across dozens of repos — work that *looked* done locally but didn't exist on any remote. | Fixed in code (best-effort `push_default_branch()` after every merge). |
| J | **Schema migration blocked the whole fleet** | A change that added `pipeline`/`pipeline_index` fields to `pipeline_state` left 11 legacy counter-only entries unhandled. The state reader crashed every cycle and **the entire fleet was blocked for hours.** | Fixed (and codified into a "data lifecycle audit" rule), but it had already cost a full outage. |
| K | **`launchd` silently killed every agent** | By default `launchd` kills *all* child processes when the managed script exits. The dispatcher spawns `claude --print` and exits, so `launchd` immediately killed every agent. No error — they just vanished. | Fixed by `AbandonProcessGroup=true` in the plist — but it's a landmine: forget it and the system *appears* to run while doing nothing. |

The pattern across this table is the real lesson: **almost every bug was a
silent failure that looked like success.** The board said `Done`, the counter
said "retrying," the agents said "REJECT pass 17," the work said "committed" —
and none of it was true. An autonomous system whose failure mode is *looking
fine* is exhausting to operate, because you can never stop watching it.

---

## Structural fragility (the stuff you can't just patch)

Beyond individual bugs, the design had load-bearing assumptions that made it
fragile by construction:

- **The verification gap.** Evaluation gates ran inside the agent's worktree.
  "Diff exists + tests pass here" is not "shipped to `main` of the right repo."
  The gap between those two statements is where unshipped work hid (Bugs G + the
  board-lied finding). A verification layer that doesn't verify the *final*
  state is theater.

- **Single machine, single subscription.** Everything ran on one Mac against one
  Claude Max account. Hit a usage ceiling or a 429 and the whole fleet throttled
  or stalled (Bug B). There was no horizontal anything.

- **`launchd` is a footgun-rich scheduler.** Three separate undocumented
  requirements (`AbandonProcessGroup=true`, a set `USER` env var for Claude Max
  OAuth, `~/.local/bin` on `PATH`) each cause *silent* total failure if missing
  (Bug K). The system can look perfectly healthy while accomplishing nothing.

- **iCloud actively corrupts git.** Storing any repo in iCloud produced
  duplicate refs (`main 2`, `agent/X 2`), empty-stderr merge failures, and
  diverged histories with no common ancestor (Bug H). The mitigation — "never
  put a dev repo in iCloud" — is real but it's an external-environment landmine
  the framework can't defend against.

- **Cross-repo blast radius.** Running across 41 repos meant one dispatcher bug
  didn't break one project, it smeared low-grade damage across all of them
  (Bug I's drift, Bug G's strandings). The audit to clean it up took a single
  uninterrupted 5-hour Opus session.

- **Operational cost vs. payoff.** Each doomed loop burned real budget
  (~$0.50/agent run on the runaway counter alone). The honest cost-benefit —
  *"too much hassle for what they're worth"* — is the actual retirement reason.
  The system needed more babysitting than the work it produced was worth.

---

## Docs don't match the code

The documentation in this repo describes an aspirational system that the shipped
code doesn't fully match. Treat the README as a design essay, not a spec.

- **Agent count: 7 documented, 4 shipped.** The README's agent table lists
  `founding-engineer`, `qa`, `skeptic`, `documentarian`, `research-analyst`,
  `architect`, and `designer`. The actual `agents/` directory contains only
  **four**: `founding-engineer`, `review`, `documentarian`, `designer`. In
  v2.2, `qa` + `skeptic` were merged into a single `review` agent and the
  `research-analyst`/`architect` stages were dropped — but the README still
  describes the old 7-agent, 8-pipeline world.

- **Version drift.** The README's "What's New" tops out at v2.1.0 / v2.0.2. The
  `CHANGELOG.md` and shipped code are at **v2.2.1**. The "Smart Classification"
  and pipeline tables in the README describe the pre-v2.2 pipeline set
  (Research → Skeptic → Architecture → …), not the simplified 3-pipeline model
  (`engineering` / `design` / `docs`) the code actually runs.

- **"Zero-token discovery" was overstated.** Early docs implied all proactive
  discovery was deterministic and free. In reality the vision-gap scanner calls
  Claude Sonnet — it is not zero-token. (`history.md` notes this correction.)

- **The README's Quick Start was never reproduced by an outside user.** It
  reads plausibly, but the system was only ever stood up on its author's
  machine. The `launchd` footguns above (Bug K) are exactly the kind of thing
  that bites a first-time installer with zero error output.

---

## Why it was retired

Not because the ideas were bad and not because it never worked — it did real
work and shipped real features. It was retired because:

1. **The failure modes were silent**, so it demanded constant supervision. An
   autonomous system you can't stop watching isn't autonomous.
2. **New failure modes appeared faster than old ones were fixed.** The
   2026-05-07 audit closed a dozen bugs and the system was retired ~18 days
   later anyway.
3. **The payoff didn't justify the babysitting.** Per the owner:
   *"too much hassle for what they're worth."*

---

## What's still worth learning from this

The corpse has useful organs. If you're building agent orchestration, these
parts held up:

- **Deterministic dispatch, stateless agents.** Letting plain Python (not an
  LLM) own polling, routing, state, and merges — and handing each agent only a
  persona and an assignment — really did keep coordination cost near zero. That
  core instinct is sound.

- **Git worktrees for isolation.** Per-agent worktrees genuinely prevented
  concurrent agents from clobbering each other. The problem was never isolation;
  it was the *merge-back-and-verify* step.

- **The most important lesson, stated plainly:** an agent saying it's done,
  a diff existing, and tests passing in a sandbox are each necessary and **none
  of them is sufficient.** "Done" must mean *the change is on the default branch
  of the repository it was meant for, and you checked.* If your verification
  doesn't assert the final shipped state, your board will lie to you too.

---

*Sources: this repo's `RETIREMENT_NOTES.md` and the 2026-05-07 dispatcher QA
audit (`QA_REPORT_2026-05-07.md`) in the private upstream, plus this repo's own
`history.md` and `CHANGELOG.md`.*
