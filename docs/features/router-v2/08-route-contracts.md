# 08 — Route Contracts

**Phase:** E1 · **Status:** [MASTER](MASTER.md) only  
**Depends on:** B4 router v1 (`relay/router.py`)

## One-liner

First-class, diffable policy artifacts (`.relay/route.json`) that declare bump
triggers, freeze fraction, pins, and call-class maps — the product is the
policy file, not a model dropdown.

## User surface

- `.relay/route.json` schema v2 (extends today’s route name file)
- `relay route show|set <name>` / env `RELAY_ROUTE`
- Validation on load; unknown keys ignored with warn in doctor later

## Acceptance (v1)

- [ ] Load/save route contract JSON with documented fields
- [ ] Precedence: CLI > repo contract > env > defaults
- [ ] Explicit model pins in contract respected
- [ ] Hermetic tests for parse/precedence/invalid

## v1 cuts

- No cryptographic signing; no cloud sync
- `relay config set-route` optional if `relay route set` ships
