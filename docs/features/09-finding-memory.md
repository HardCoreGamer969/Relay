# 09 — Finding-Driven Memory

**Phase:** A3 · **Status:** [MASTER roadmap table](MASTER.md) only  
**Shipped:** features-revamp (`.relay/memory.json` + `relay memory` + `/memory`)  
**Depends on:** three-pool MemoryBus Stage 1 (exists). Hands Stage 2 read deferred.

## Blockers

- None hard for durable shared pool; Stage 2 hands private read remains deferred

## Locked decisions

| Topic | Decision |
|-------|----------|
| Location | Repo-local `.relay/memory.json` only |
| What persists | Shared pool only (never brain/hands private) |
| Stage 2 | Deferred |

## Acceptance criteria

- [x] Shared findings survive process exit and load on next run
- [x] User can pin/forget via CLI (`relay memory`) and TUI `/memory`
- [x] Budget trim prefers pinned entries
- [x] Brain private pool never written to durable file
- [x] Hermetic tests
