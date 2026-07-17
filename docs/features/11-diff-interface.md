# 11 — Diff-as-Interface / Surgical Commit Mode

**Shipped:** features-revamp (`--confirm-diff` / `--commit-per-step` / `relay rewind <step>`)  
**Phase:** D3 · **Status:** [MASTER roadmap table](MASTER.md) only  
**Depends on:** reliable step boundaries; stronger with D2 checkpoints

## Blockers

- Soft: shared checkpoint machinery with #3 — landed with D2
- v1 git rewind requires a clean git repo (documented; no shadow-copy fallback)

---

## One-liner

Hands don’t “finish” a step until the brain or user accepts a **step-scoped diff**; optional auto-commit per step; `relay rewind <step>` restores tree + plan cursor.

## Why it sets Relay apart

Feels like reviewing a junior’s PR *per task*, not one mega-diff at the end. Aligns with bounded steps and no-op patch honesty.

## User surface

- `--confirm-diff` / `RELAY_CONFIRM_DIFF` / config `diff.confirm` — after each successful step, unified diff of touched paths; accept/reject via `user_decision`
- Reject → step failed with reason → brain replan
- `--commit-per-step` / `RELAY_COMMIT_PER_STEP` — git commit message from step instruction (requires git)
- `relay rewind <step-id>` — `git checkout --` touched paths from checkpoint metadata; fails clearly outside a git repo

## Hooks into existing code

- `relay/diff_iface.py` — diffs, confirm, commit, rewind
- Orchestrator `_confirm_then_settle` after supervised accept
- D2 checkpoints store `step_touches` for rewind lookup

## Acceptance criteria

- [x] Step completion can require explicit diff accept when enabled
- [x] Reject returns control to brain replan with rejection reason
- [x] Rewind restores files for that step’s touches (documented git requirement)
- [x] Tests cover accept/reject without needing a real LLM

## Open questions

- Require clean git repo for v1, or support a Relay-side shadow copy? → **git required for rewind/commit-per-step; hermetic diffs use before-snapshots**
- Brain auto-accept on mechanical steps vs always ask? → always ask when `--confirm-diff`

## Out of scope (v1)

- Full interactive diff editor inside the TUI
