# 01 — Model Bake-Off (`relay duel`)

**Shipped:** features-revamp (sequential `relay duel`, `.relay/duels/` scorecards)  
**Phase:** C1 · **Status:** [MASTER roadmap table](MASTER.md) only  
**Depends on:** A1 cost receipts; solid per-role telemetry (ideally B4 router metrics)

## Blockers

- Optional: `provider` on `CallRecord` — [REVAMP Phase 4](../REVAMP.md) bake-off telemetry
- Worktree / isolation story must be decided before parallel duels

---

## One-liner

Run the same goal across N brain×hands pairings; score completion, steps, cost, escalations, and diff quality; emit a shareable scorecard.

## Why it sets Relay apart

Competitors pick *a* model. Relay already meters brain vs hands. Bake-off turns that into proof: “Sonnet brain + Haiku hands beat Opus solo at a fraction of the cost on *this* repo.”

## User surface

- CLI: `relay duel -g "<goal>" --pair brain=…,hands=…` (repeatable) or `--matrix` file
- `relay duel --list` shows persisted scorecards under `.relay/duels/`
- v1: **sequential only**; same-tree with `git checkout`/`git clean` between pairings; dirty tree at start fails closed
- Output: table + JSON under `.relay/duels/`

## Hooks into existing code

- `relay/duel.py` → `run_planned` (same orchestrator path)
- `.relay/duels/*.json` scorecards (status, steps, $, escalations, wall time)
- A1 cost receipts via per-pairing `Ledger`

## Acceptance criteria

- [x] Two pairings can run the same goal with clean revert (git restore between pairings)
- [x] Scorecard includes at least: terminal status, steps, $, escalations, wall time
- [x] Results persist and are listable without re-running
- [x] Hermetic tests with mocked clients cover scoring math and matrix parsing

## Open questions

- Worktree isolation vs in-place + git reset — **v1: same-tree + git restore; parallel deferred**
- How much “diff quality” is heuristic vs LLM-judge (costly)?
- Should solo mode be a first-class matrix cell?

## Out of scope (v1)

- Public hosted leaderboard
- Auto-merging the winning diff into the user’s branch without confirmation
- Parallel pairings / git worktrees
