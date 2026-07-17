# U2 — Cockpit Chrome

**Stage:** U2 · **Status:** [MASTER](MASTER.md) only  
**Depends on:** U0–U1 · decisions in [DECISIONS.md](DECISIONS.md)

## One-liner

Turn the working view into an **IDE cockpit**: status rail + plan dock +
stream, with first-class cost and always-on route.

## Surfaces

### Status rail (always during run)

- Phase LED + word (`PLANNING` / `AWAITING YOU` / `EXECUTING` / …)
- `step N/M` + truncated active instruction
- **Cost first-class:** `$spent` or `$spent / $remaining` when envelope set;
  escalate style at warn thresholds ([DECISIONS §3](DECISIONS.md))
- **Route always:** `route=balanced` (+ `freeze*` when frozen)
- Context % when window known
- Short brain/hands slugs · queue · hints

### Plan dock

- Default `full` with **active highlight**; `/plan` + `RELAY_TUI_PLAN`
- Narrow auto → `active`
- Updates in place from the same plan events as today’s in-stream block
  (stream may keep a compact “plan committed” line; dock is source of truth)

### Theme stub

- Apply website tokens (bg/red/text/warn/border) to chrome
- Role colors: brain=red, hands=dim text (site), findings=success/warn as needed

## Acceptance

- [ ] Live run shows rail facts without opening `/cost` or `/route`
- [ ] Plan dock defaults to full + active highlight; modes switchable
- [ ] Envelope warn thresholds change cost slot style once each
- [ ] Hermetic tests for rail segments + plan modes (headless mirrors)

## v1 cuts

- No Markdown/diffs yet (U3)
- Logo welcome can wait for U6 if theme lands here partially
- Animations still minimal (LED may stay; full motion system is U5)
