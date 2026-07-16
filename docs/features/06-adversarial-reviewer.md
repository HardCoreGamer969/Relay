# 06 — Adversarial Reviewer

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

- `relay run --skeptic` / config `review.adversarial = true`
- Optional dedicated skeptic model (defaults to brain model with attacker system prompt)
- TUI: skeptic findings as a distinct color/channel; require dismiss, fix, or replan
- Verdicts gate step acceptance when enabled

## Hooks into existing code

- `investigation.py` read-only brain loop
- Step reviewer in `planner.py` (fix fail-open issues per REVAMP if still relevant)
- Finding channel (`<finding>`) and shared memory
- Telemetry: skeptic cost as its own role or tagged brain calls

## Acceptance criteria

- [ ] With `--skeptic`, a plan or step can be blocked on unresolved skeptic objections
- [ ] Skeptic is read-only (no edit/bash) — same invariant as brain
- [ ] Cost attributed separately in the run receipt
- [ ] Tests with scripted skeptic objections → replan / user dismiss paths

## Open questions

- Skeptic on every step vs only on plan + escalations (cost)?
- Same model vs forced different model for diversity?

## Out of scope (v1)

- Multi-skeptic panels / majority vote
