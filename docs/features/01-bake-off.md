# 01 — Model Bake-Off (`relay duel`)

**Phase:** C1 · **Status:** planned · **Depends on:** A1 cost receipts, solid per-role telemetry (ideally B4 router metrics)

[← Master plan](MASTER.md)

---

## One-liner

Run the same goal across N brain×hands pairings; score completion, steps, cost, escalations, and diff quality; emit a shareable scorecard.

## Why it sets Relay apart

Competitors pick *a* model. Relay already meters brain vs hands. Bake-off turns that into proof: “Sonnet brain + Haiku hands beat Opus solo at a fraction of the cost on *this* repo.”

## User surface

- CLI: `relay duel -g "<goal>" --matrix brain=…,hands=…` (repeatable pairs) or a named matrix file
- Optional: sequential vs parallel (parallel only when safe isolation exists)
- Output: table + JSON under `.relay/duels/`; link from `relay runs`
- Later: repo-default recommendation from past duels (“this codebase likes cheap hands”)

## Hooks into existing code

- `telemetry.py` / end-of-run brain vs hands cost table
- `.relay/runs.jsonl` (`runlog.py`) as the comparison substrate
- Same orchestrator path as `relay run` — no second agent loop
- Add `provider` on `CallRecord` if not already present (see REVAMP Phase 4)

## Acceptance criteria

- [ ] Two pairings can run the same goal with isolated worktrees or clean revert
- [ ] Scorecard includes at least: terminal status, steps, $, escalations, wall time
- [ ] Results persist and are listable without re-running
- [ ] Hermetic tests with mocked clients cover scoring math and matrix parsing

## Open questions

- Worktree isolation vs in-place + git reset — what’s the v1 safety story?
- How much “diff quality” is heuristic vs LLM-judge (costly)?
- Should solo mode be a first-class matrix cell?

## Out of scope (v1)

- Public hosted leaderboard
- Auto-merging the winning diff into the user’s branch without confirmation
