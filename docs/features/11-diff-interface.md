# 11 — Diff-as-Interface / Surgical Commit Mode

**Phase:** D3 · **Status:** [MASTER roadmap table](MASTER.md) only  
**Depends on:** reliable step boundaries; stronger with D2 checkpoints

## Blockers

- Soft: shared checkpoint machinery with #3 — prefer landing D2 first or extracting a common checkpoint primitive
- v1 likely requires a clean git repo (document; don’t half-implement shadow copies)

---

## One-liner

Hands don’t “finish” a step until the brain or user accepts a **step-scoped diff**; optional auto-commit per step; `relay rewind <step>` restores tree + plan cursor.

## Why it sets Relay apart

Feels like reviewing a junior’s PR *per task*, not one mega-diff at the end. Aligns with bounded steps and no-op patch honesty.

## User surface

- After each step (or on `--confirm-diff`): show unified diff of touched paths
- Accept / reject / edit-request → continue / replan
- `--commit-per-step` with message from step text
- `relay rewind <step-id>` using checkpoints (git preferred)

## Hooks into existing code

- Tool touch-path reporting / `_StepOutcome`
- Read-before-edit / content hashes
- Run checkpoints (shared need with plan fork #3)
- TUI present_prompt chokepoint for accept/reject

## Acceptance criteria

- [ ] Step completion can require explicit diff accept when enabled
- [ ] Reject returns control to brain replan with rejection reason
- [ ] Rewind restores files for that step’s touches (documented git requirement)
- [ ] Tests cover accept/reject without needing a real LLM

## Open questions

- Require clean git repo for v1, or support a Relay-side shadow copy?
- Brain auto-accept on mechanical steps vs always ask?

## Out of scope (v1)

- Full interactive diff editor inside the TUI
