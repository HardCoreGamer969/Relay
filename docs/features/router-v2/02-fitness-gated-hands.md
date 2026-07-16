# 02 — Fitness-Gated Hands

**Phase:** E9 · **Status:** [MASTER](MASTER.md) only  
**Depends on:** C2 probe fitness concepts; parse_failures telemetry

## One-liner

Hands stay economy until harness proves protocol failure (parse spiral,
malformed tags); then bump hands one tier for N steps and de-escalate.

## User surface

- Contract: `hands_bump_on_parse_failures: 3`, `hands_bump_steps: 2`
- `route_change` reason `fitness_bump` / `fitness_decay`
- Uses live parse_failure counter (offline probe optional for defaults)

## Acceptance (v1)

- [ ] Parse-failure threshold triggers hands bump
- [ ] Auto de-escalate after clean streak / N steps
- [ ] Envelope freeze still blocks bumps
- [ ] Tests with injected parse failures

## v1 cuts

- No full `relay probe` mid-run; use parse_failures as proxy
