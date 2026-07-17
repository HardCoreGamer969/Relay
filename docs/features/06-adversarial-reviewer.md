# 06 — Adversarial Reviewer

**Shipped:** features-revamp (`--skeptic` / `RELAY_SKEPTIC` / `review.adversarial`)  
**Phase:** D1 · **Status:** [MASTER roadmap table](MASTER.md) only  
**Depends on:** investigation/reviewer loop; stronger with B2 firewall + B4 router

## Blockers

- Soft: reviewer fail-open / investigation terminator correctness — see [REVAMP](../REVAMP.md) confirmed bugs / Phase 0 follow-ups if still open

---

## One-liner

Optional second brain (or attacker prompt) that only tries to kill the plan: missing tests, destructive bash, scope creep, silent API breaks — must be answered before hands continue.

## Why it sets Relay apart

“Two brains, one pair of hands”: planner + skeptic, executor stays cheap and narrow. Fits Relay’s selective brain re-engagement better than constant full-agent debate.

## User surface

- `relay run --skeptic` / env `RELAY_SKEPTIC=1` / config `review.adversarial = true`
- Defaults to brain model with attacker system prompt (`relay/skeptic.py`)
- Unresolved objections: one forced replan, then user dismiss, else `skeptic_blocked`
- Cost attributed via `CallRecord.purpose="skeptic"`

## Hooks into existing code

- `investigation.py` read-only brain loop
- Plan-level pass in `run_planned` (v1; per-step skeptic deferred)
- Telemetry: `purpose=skeptic` on ledger records

## Acceptance criteria

- [x] With `--skeptic`, a plan can be blocked on unresolved skeptic objections
- [x] Skeptic is read-only (no edit/bash) — same invariant as brain
- [x] Cost attributed separately in the run receipt (`purpose=skeptic`)
- [x] Tests with scripted skeptic objections → replan / user dismiss paths

## Open questions

- Skeptic on every step vs only on plan + escalations (cost)? — **v1: plan only**
- Same model vs forced different model for diversity?

## Out of scope (v1)

- Multi-skeptic panels / majority vote
- Per-step skeptic (plan-level only)
