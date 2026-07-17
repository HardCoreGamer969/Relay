# 10 — Orchestra Mode

**Shipped:** features-revamp (`--orchestra N` + path leases + `hands-N` telemetry)  
**Phase:** D4 · **Status:** [MASTER roadmap table](MASTER.md) only  
**Depends on:** A3 memory, B2 firewall, stable step boundaries; ideally D2 forks

## Blockers

- Soft: bridge cancel/join under concurrency — v1 joins thread-pool workers on cancel (money-leak safe: never tear mid-call)
- Isolation: **file leases on a single tree** (not worktrees)

---

## One-liner

One brain conducts N hands workers on independent plan steps (disjoint files); conflict detector merges contested paths back to the brain.

## Why it sets Relay apart

“Many full agents” parallelism elsewhere. Relay’s narrow step context enables **true parallel hands** without each worker drowning in the full plan.

## User surface

- `relay run --orchestra 3` (max parallel hands)
- Parallel only for steps with extractable, pairwise-disjoint path claims; no-claim / overlapping steps stay serial
- Runtime `PathLease` refuses a second write to a claimed path → serialize via replan
- Cancel joins workers (`ThreadPoolExecutor.shutdown(wait=True)`)
- Telemetry: worker calls use role `hands-1` / `hands-2` (model still resolves to hands); single shared ledger

## Hooks into existing code

- `relay/orchestra.py` — claims, leases, batch select, parallel runner
- `run_planned(..., orchestra_workers=N)`
- `ModelConfig.canonical_role` maps `hands-N` → hands model
- Thread-safe `Ledger.add`

## Acceptance criteria

- [x] Two hands can complete disjoint file steps without sharing full plan text
- [x] Overlapping path claims are detected before second write
- [x] Cancel stops all workers and joins cleanly (no orphan bash)
- [x] Telemetry attributes cost per worker (`hands-N` roles on one ledger)
- [x] Concurrency tests with mocked models

## Open questions

- Threads vs processes vs subprocess workers? → **threads + file leases (v1)**
- Git worktrees per worker vs single tree + file leases? → **single tree + leases**

## Out of scope (v1)

- Distributed multi-machine orchestra
- Hands workers with different models each (can follow once router exists)
- Per-step brain supervise inside the parallel batch (reviews stay serial / skipped for the batch)
