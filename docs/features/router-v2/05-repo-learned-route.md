# 05 — Repo-Learned Route
**Shipped:** router-v2 thin v1 (see PROGRESS.md)


**Phase:** E11 · **Status:** [MASTER](MASTER.md) only  
**Depends on:** C1 duels; runlog; E1 contracts

## One-liner

From `.relay/duels/` + runs: suggest this repo’s sweet-spot route. Local only.

## User surface

- `relay route recommend` prints suggested route + evidence
- Optional auto-write `.relay/route.json` with `--apply`
- Never cloud; never silent apply without flag

## Acceptance (v1)

- [ ] Recommend from duel scorecards when present
- [ ] Fallback: most-used successful route in runlog
- [ ] Tests with fixture duel JSON

## v1 cuts

- No ML; simple heuristics (lowest $ among completed duels, etc.)
