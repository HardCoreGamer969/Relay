# 03 — Plan Time-Travel / Fork Studio

**Shipped:** features-revamp (`.relay/forks/` + `.relay/checkpoints/` + `relay fork` / `--resume`)  
**Phase:** D2 · **Status:** [MASTER roadmap table](MASTER.md) only  
**Depends on:** A2 `/why` (helpful); durable checkpoints

## Blockers

- Soft: full `RunState` / session resume — [REVAMP Phase 2](../REVAMP.md). v1 resumes via plan cursor + completed steps only (no transcript/ledger restore).

---

## One-liner

Fork a plan into alternate futures (minimal fix vs refactor vs test-first), compare them, execute one, and resume from any completed-step checkpoint.

## Why it sets Relay apart

Session resume is table stakes. Relay’s explicit `<plan>` + bounded replan makes **branching intent** natural — git for plans, not only files.

## User surface

- `relay fork list|save <name>|load <name>` — named forks under `.relay/forks/`
- `relay run --save-fork <name>` — persist committed plan before/during execution
- `relay run --fork <name>` / `--resume <checkpoint|latest>` — execute a fork or resume at cursor
- Step-boundary checkpoints under `.relay/checkpoints/` (plan JSON + completed set + touches)
- `relay rewind --checkpoint <id>` — inspect cursor / resume hint

## Hooks into existing code

- `relay/plan_fork.py` — fork + checkpoint persistence
- `run_planned(committed_plan=..., auto_checkpoint=..., save_fork_as=...)`
- Planner `Plan.to_state` / `from_state` (completed steps stay marked on resume)

## Acceptance criteria

- [x] User can create ≥2 named plan forks from one goal without executing
- [x] Executing fork B does not destroy fork A’s plan text
- [x] Checkpoint at step boundaries restores plan cursor + completed set
- [x] Tests cover fork metadata persistence and resume cursor

## Open questions

- Are forks only plan-text, or do they include workspace snapshots? → **v1: plan-text (+ optional git hash on checkpoint)**
- Max forks / retention policy under `.relay/`? → unbounded for now

## Out of scope (v1)

- Automatic multi-fork execution (that’s closer to bake-off / orchestra)
- Full RunState (transcript, ledger, memory) resume
