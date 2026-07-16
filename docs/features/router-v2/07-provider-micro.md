# 07 — Provider Micro-Routing
**Shipped:** router-v2 thin v1 (see PROGRESS.md)


**Phase:** E7 · **Status:** [MASTER](MASTER.md) only  
**Depends on:** E2 call-class; OpenRouter provider extras

## One-liner

Relay picks the **model class**; OpenRouter picks the **barn** — append
`:floor` / pass `max_price` under the policy without silent model swaps.

## User surface

- Contract: `provider_sort: floor|nitro|default` per call-class or global
- Optional `max_price` for floor routes
- Visible in `/why` as provider hint (not a model change)

## Acceptance (v1)

- [ ] Router can attach provider routing extras to OpenRouter calls
- [ ] Does not change resolved model slug (except `:floor` suffix when enabled)
- [ ] Tests for suffix/extra_body construction; non-OpenRouter no-op

## v1 cuts

- No BYOK economics UI; OpenRouter-first
