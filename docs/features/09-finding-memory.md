# 09 — Finding-Driven Memory

**Phase:** A3 · **Status:** planned · **Depends on:** three-pool MemoryBus Stage 1 (exists); Stage 2 hands read still deferred but not required for v1 persist

[← Master plan](MASTER.md)

---

## One-liner

Elevate `<finding>` / shared directives into a curated, durable project scratchpad of *decisions* — not dumped chat history.

## Why it sets Relay apart

Competitors RAG the world or replay transcripts. Relay’s three-pool design wants a **small, role-aware coordination channel**. Persist the useful residue across runs.

## User surface

- Auto-capture high-signal findings/directives into `.relay/memory.json` (name TBD)
- `/memory` list · pin · edit · forget
- Brain reads durable shared memory at plan time; hands sees pinned directives (via shared pool)
- Examples: “auth lives in X”, “don’t touch migrations without Y”, “prefer apply_patch”

## Hooks into existing code

- `memory.py` MemoryBus / PlanMemory
- Finding channel from executor/brain
- TUI slash commands pattern
- Optional merge with constraint-card idea from firewall (#5)

## Acceptance criteria

- [ ] Shared findings can survive process exit and load on next run in same cwd/repo
- [ ] User can pin/forget entries without hand-editing JSON (slash or CLI)
- [ ] Budget caps still apply; durable store is curated, not infinite append
- [ ] Brain never leaks private brain-pool entries into durable shared file
- [ ] Tests for load/save/pin/forget and budget trim

## Open questions

- Repo-local only vs user-global store?
- Hands Stage 2 private pool persistence — same release or later?

## Out of scope (v1)

- Embedding-based semantic retrieval over the whole repo
