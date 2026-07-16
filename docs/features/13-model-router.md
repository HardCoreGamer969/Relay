# 13 — Model Router (brand-defining)

**Phase:** B4 · **Status:** [MASTER roadmap table](MASTER.md) only  
**Shipped:** features-revamp (`relay/router.py` + replan bump/freeze + `route_change` events)  
**Depends on:** A1 cost envelope, A2 `/why`, B2 escalation classes; `call_model(role, …)` seam exists

## Blockers

- None hard beyond A1/A2/B2 for a solid v1
- Catalog tier tables may later lean on [REVAMP Phase 4](../REVAMP.md) catalog de-rot — not a v1 gate

---

## One-liner

Relay is a **model router**, not a chat app: bind and *re-bind* models to roles mid-run so thinking escalates only when needed and hands stay cheap until protocol fitness or failure demands otherwise.

## Why this is the brand

Claude Code / Codex / OpenCode ask: “which model are you chatting with?”  
Relay asks: “which **orchestration policy** spends your dollars across brain and hands?”

The seam `call_model(role, …)` already makes provider/model a config concern. The router makes **policy** the product: spend smart by default, prove it with receipts and `/why`.

## Core behavior (v1)

1. **Static policy** — named routes: `economy` | `balanced` (default) | `premium` mapping brain/hands slugs.
2. **Escalation triggers** — on replan: optionally bump **brain** model one tier for that call only.
3. **De-escalation** — deferred (v1 keeps bumps call-scoped; no sticky bump state).
4. **Hard invariants** — hands never silently become the brain; router never bypasses cost envelope; every route change is a `route_change` event for `/why`.
5. **Freeze** — at ≥80% of the cost envelope, bumps freeze (`bump_frozen`).

## Non-goals (keep the brand sharp)

- Not a multi-turn “chat with GPT-x” session switcher
- Not automatic shopping across every OpenRouter slug without a policy
- Not replacing user-selected explicit model overrides (`RELAY_BRAIN_MODEL` still wins)

## User surface

- `relay run --route economy` / env `RELAY_ROUTE` / `.relay/route.json`
- Explicit `RELAY_BRAIN_MODEL` / `RELAY_HANDS_MODEL` / config role models beat the router
- `/why` includes route change reasons (no new LLM calls)
- (TUI `/route`, counterfactual $ saved deferred)

## Hooks into existing code

- `relay/router.py` — thin policy layer before/around `call_model` via per-call `ModelConfig`
- `config.py` / env precedence — route defaults beneath explicit overrides
- Orchestrator replan seam — emit `route_change` events
- Explain harness — `route_changes` section

## Policy sketch (v1 shipped)

| Event | Action |
|-------|--------|
| Run start | Bind models from route profile (under explicit overrides) |
| Replan after hands failure | Brain bump one tier for replan call only |
| Cost envelope 80% | Freeze bumps; prefer current bindings |
| User env/config model override | Pin role; router may not override |

## Acceptance criteria

- [x] Route profiles resolve brain/hands models deterministically with documented precedence
- [x] Mid-run brain bump on replan is visible in trace + `/why`
- [x] Explicit env/config model overrides still beat the router
- [x] Cost envelope can freeze escalation
- [x] `/why` includes route change reasons without new LLM calls
- [x] Tests: precedence + bump/freeze

## Open questions

- Tier lists: maintain mapping tables in-repo vs derive from catalog pricing/context? **v1: in-repo placeholders.**
- Should skeptic (#6) use a fixed cheap model always?
- Counterfactual “$ saved” — **deferred past v1.**

## Out of scope (v1)

- Fully autonomous per-token bandit optimization across the whole catalog
- Routing user chat turns (there is no general chat product)
- Hands bump on malformed tags / clean-streak de-escalation
- `relay config set-route` CLI helper (env + `--route` + repo file ship)

## Success metric

A default `economy` route completes representative tasks with materially lower $ than `premium`, with `/why` able to show *when* bumps happened — and bake-off (#1) can validate the policy on real repos.
