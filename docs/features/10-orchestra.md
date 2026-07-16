# 10 — Orchestra Mode

**Phase:** D4 · **Status:** planned · **Depends on:** A3 memory, B2 firewall, stable step boundaries; ideally D2 forks for contested slices

[← Master plan](MASTER.md)

---

## One-liner

One brain conducts N hands workers on independent plan steps (disjoint files); conflict detector merges contested paths back to the brain.

## Why it sets Relay apart

“Many full agents” parallelism elsewhere. Relay’s narrow step context enables **true parallel hands** without each worker drowning in the full plan.

## User surface

- `relay run --orchestra 3` (max parallel hands)
- TUI: per-worker cyan lanes or tagged tool lines (`hands-2`)
- Hard stop when two workers claim overlapping paths — brain replans contested slice
- Envelope cost multiplies carefully (show projected $)

## Hooks into existing code

- Orchestrator step scheduling (today serial)
- Bridge cancel/join money-leak guards (critical under concurrency)
- Shared memory findings/directives
- Policy/bash locking across workers

## Acceptance criteria

- [ ] Two hands can complete disjoint file steps without sharing full plan text
- [ ] Overlapping path claims are detected before second write
- [ ] Cancel stops all workers and joins cleanly (no orphan bash)
- [ ] Telemetry attributes cost per worker
- [ ] Concurrency tests with mocked models

## Open questions

- Threads vs processes vs subprocess workers?
- Git worktrees per worker vs single tree + file leases?

## Out of scope (v1)

- Distributed multi-machine orchestra
- Hands workers with different models each (can follow once router exists)
