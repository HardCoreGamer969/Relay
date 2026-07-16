# 13 — Model Router (brand-defining)

**Phase:** B4 · **Status:** planned · **Depends on:** A1 cost envelope, A2 `/why` (to explain routes), B2 escalation classes useful for triggers; static `call_model(role, …)` seam already exists

[← Master plan](MASTER.md)

---

## One-liner

Relay is a **model router**, not a chat app: bind and *re-bind* models to roles mid-run so thinking escalates only when needed and hands stay cheap until protocol fitness or failure demands otherwise.

## Why this is the brand

Claude Code / Codex / OpenCode ask: “which model are you chatting with?”  
Relay asks: “which **orchestration policy** spends your dollars across brain and hands?”

The seam `call_model(role, …)` already makes provider/model a config concern. The router makes **policy** the product: spend smart by default, prove it with receipts and `/why`.

## Core behavior (v1)

1. **Static policy** — named routes: e.g. `economy` (Haiku hands, mid brain), `balanced`, `premium` (big brain, mid hands).
2. **Escalation triggers** — on replan, repeated_step risk, protocol parse failures, or `tech`/`product` escalations: optionally bump **brain** model one tier for the next planning call only.
3. **De-escalation** — after a clean streak of steps, return to default hands/brain binding.
4. **Hard invariants** — hands never silently become the brain; router never bypasses cost envelope; every route change is a trace event for `/why`.

## Non-goals (keep the brand sharp)

- Not a multi-turn “chat with GPT-x” session switcher
- Not automatic shopping across every OpenRouter slug without a policy
- Not replacing user-selected explicit model overrides (`RELAY_BRAIN_MODEL` still wins)

## User surface

- `relay config set-route economy|balanced|premium` + repo `.relay/route.toml`
- `relay run --route economy`
- TUI: status shows `brain: slug→slug` when a bump happens; `/route` inspects policy
- Receipt: $ saved vs “premium-everything” counterfactual estimate (honest about being approximate)
- `/why` explains the last route change

## Hooks into existing code

- `models.py` `call_model(role, …)` — resolution becomes policy-aware per call
- `config.py` / env precedence — insert route defaults beneath explicit overrides
- Orchestrator escalation / replan seams — emit `RouteChange` events
- Telemetry `CallRecord` — store resolved model + reason code (`default`, `replan_bump`, …)
- Catalog fitness (#8) and duel (#1) later feed route recommendations

## Policy sketch (draft)

| Event | Action |
|-------|--------|
| Run start | Bind models from route profile |
| Replan after hands `<blocked>` | Brain bump one tier for replan call |
| Repeated malformed tags from hands | Hands bump or protocol nudge; count toward envelope |
| N successful steps | Revert bumps to route defaults |
| Cost envelope 80% | Freeze bumps; prefer cheaper bindings |
| User `/model` override | Pin role; router may not override until unpin |

## Acceptance criteria

- [ ] Route profiles resolve brain/hands models deterministically with documented precedence
- [ ] Mid-run brain bump on replan is visible in TUI + trace + receipt
- [ ] Explicit env/config model overrides still beat the router
- [ ] Cost envelope can freeze escalation
- [ ] `/why` includes route change reasons without new LLM calls
- [ ] Tests: policy table × events with mocked catalog

## Open questions

- Tier lists: maintain mapping tables in-repo vs derive from catalog pricing/context?
- Should skeptic (#6) use a fixed cheap model always?
- Counterfactual “$ saved” — ship in v1 or wait for duel data?

## Out of scope (v1)

- Fully autonomous per-token bandit optimization across the whole catalog
- Routing user chat turns (there is no general chat product)

## Success metric

A default `economy` route completes representative tasks with materially lower $ than `premium`, with `/why` able to show *when* bumps happened — and bake-off (#1) can validate the policy on real repos.
