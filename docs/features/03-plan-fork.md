# 03 — Plan Time-Travel / Fork Studio

**Phase:** D2 · **Status:** [MASTER roadmap table](MASTER.md) only  
**Depends on:** A2 `/why` (helpful); durable checkpoints

## Blockers

- Hard: `RunState` / session resume — [REVAMP Phase 2](../REVAMP.md)

---

## One-liner

Fork a plan into alternate futures (minimal fix vs refactor vs test-first), compare them, execute one, and resume from any completed-step checkpoint.

## Why it sets Relay apart

Session resume is table stakes. Relay’s explicit `<plan>` + bounded replan makes **branching intent** natural — git for plans, not only files.

## User surface

- After plan (or on interrupt): `fork` → name branches A/B/C with different brain briefs
- Side-by-side plan diff in TUI or `--plan-only` style CLI output
- `relay run --resume <checkpoint>` / pick fork + step cursor
- Keep non-executed forks as alternate futures until discarded

## Hooks into existing code

- Planner plan emission + replan-tail logic in `orchestrator.py`
- Dual-fidelity plan memory / transcript
- Bridge interrupt → steer path (natural fork moment)
- Needs serialized run state (align with REVAMP `RunState`)

## Acceptance criteria

- [ ] User can create ≥2 named plan forks from one goal without executing
- [ ] Executing fork B does not destroy fork A’s plan text
- [ ] Checkpoint at step boundaries restores plan cursor + completed set
- [ ] Tests cover fork metadata persistence and resume cursor

## Open questions

- Are forks only plan-text, or do they include workspace snapshots?
- Max forks / retention policy under `.relay/`?

## Out of scope (v1)

- Automatic multi-fork execution (that’s closer to bake-off / orchestra)
