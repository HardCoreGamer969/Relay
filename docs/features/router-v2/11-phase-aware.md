# 11 — Phase-Aware Routes
**Shipped:** router-v2 thin v1 (see PROGRESS.md)


**Phase:** E8 · **Status:** [MASTER](MASTER.md) only  
**Depends on:** E1–E2; orchestrator phases (planning/executing)

## One-liner

Different bind policies for planning vs execution vs review/diff — router as
a state machine over the run.

## User surface

- Contract `phases.planning|execution|review` → route or call-class overrides
- Defaults: plan=mid brain; execute=cheap hands; review=mid/cheap
- Phase changes emit `route_change`

## Acceptance (v1)

- [ ] Phase set at planning start / execution start
- [ ] Model resolution consults phase map
- [ ] Tests for phase transitions

## v1 cuts

- Diff-accept phase optional; map to review if present
