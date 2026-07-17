# 04 — Counterfactual Receipt
**Shipped:** router-v2 thin v1 (see PROGRESS.md)


**Phase:** E4 · **Status:** [MASTER](MASTER.md) only  
**Depends on:** A1 receipt; catalog pricing when available

## One-liner

End-of-run: what this goal would have cost on `premium` (or a named baseline)
vs actual — the marketing screenshot chat apps can’t print honestly.

## User surface

- Receipt lines: `actual $X · premium-counterfactual $Y · saved ~$Z (approx)`
- Flag `--counterfactual premium` (default on when route ≠ premium)
- Honest “approx” labeling from catalog $/token × tokens

## Acceptance (v1)

- [ ] Counterfactual computed without extra model calls
- [ ] Shown in CLI receipt; stored on runlog/envelope snap when cheap
- [ ] Tests with fixed prices / token counts

## v1 cuts

- Catalog miss → skip with “unknown”; no dual live calls
