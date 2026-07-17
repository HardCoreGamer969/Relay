# 10 — Shadow Routing
**Shipped:** router-v2 thin v1 (see PROGRESS.md)


**Phase:** E12 · **Status:** [MASTER](MASTER.md) only  
**Depends on:** E2; envelope budget

## One-liner

Opt-in learn mode: log what a cheaper call-class *would* have used (or rarely
dual-call under a tiny budget) to feed repo-learned routes — never default-on.

## User surface

- `RELAY_SHADOW_ROUTE=1` / `--shadow-route`
- Shadow decisions logged to `.relay/shadow.jsonl` (model choice only by default)
- Dual-call only if `shadow.dual_call=true` AND remaining envelope > reserve

## Acceptance (v1)

- [ ] Default shadow = log-only counterfactual choice (no second API call)
- [ ] Dual-call path budget-capped and off by default
- [ ] Tests for log-only path

## v1 cuts

- No scoring of shadow quality beyond protocol parse if dual-call enabled
