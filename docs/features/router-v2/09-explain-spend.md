# 09 — Explain the Spend
**Shipped:** router-v2 thin v1 (see PROGRESS.md)


**Phase:** E5 · **Status:** [MASTER](MASTER.md) only  
**Depends on:** A2 `/why`; route_change events; E4 optional

## One-liner

`/why spend` — dollar timeline with route reason codes; exportable, redacted.

## User surface

- TUI `/why spend` or `/why` section “Spend”
- CLI `relay runs --explain <id>` includes spend section when harness has it
- Zero new model tokens

## Acceptance (v1)

- [ ] Spend timeline from ledger + route_change events
- [ ] Redacted like `/log`
- [ ] Hermetic tests

## v1 cuts

- No interactive chart; Markdown/text only
