# 01 — Call-Class Routing

**Phase:** E2 · **Status:** [MASTER](MASTER.md) only  
**Depends on:** E1 route contracts; `CallRecord.purpose` where present

## One-liner

Route by **purpose** (`plan|replan|review|answer|skeptic|hands_step|compact`),
not only by brain/hands role — upgrade the kind of thought, not the chat.

## User surface

- Contract field `call_class` → model tier or slug
- Defaults: compact/hands cheap; plan/review mid; replan/product hot
- Every resolution emits `route_change` with `purpose`

## Acceptance (v1)

- [ ] Purpose-aware model resolution in router
- [ ] Orchestrator/planner pass purpose into call path
- [ ] Tests for purpose→tier map + override pins

## v1 cuts

- No ML classifier of free-text purpose; harness tags only
