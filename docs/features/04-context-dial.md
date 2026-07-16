# 04 — Hands Context Dial

**Phase:** B3 · **Status:** [MASTER roadmap table](MASTER.md) only  
**Depends on:** A2 helpful for proving “narrow wins”; pairs with B4 router

## Blockers

- None

---

## One-liner

Expose executor narrowness as a user control and show how much context the hands *didn’t* see — amnesia as a feature.

## Why it sets Relay apart

Everyone races to stuff more context in. Relay’s moat is **less** context for hands. Make it visible and tunable.

## Dial levels (draft)

| Level | Hands sees |
|-------|------------|
| `needle` | Current step + one-line carry-over (today’s default) |
| `findings` | Above + shared findings/directives |
| `summary` | Above + compact summaries of prior steps |
| `wide` | Debug mode: more transcript (expensive, distractible) |

## User surface

- `RELAY_HANDS_CONTEXT_MODE` / `--hands-context needle|findings|summary|wide`
- TUI status: context mode + optional “tokens withheld” estimate
- End-of-run: compare cost/quality notes when mode ≠ needle

## Hooks into existing code

- Narrow executor prompt assembly in `orchestrator.py`
- `MemoryBus` shared/hands pools (`memory.py`)
- Telemetry for tokens per role

## Acceptance criteria

- [ ] Four modes change hands prompt contents in tests (snapshot or substring)
- [ ] Default remains `needle` (preserve architecture promise)
- [ ] Status/help documents that `wide` is for debugging, not recommended default
- [ ] No hands access to full brain reasoning even in `wide` (hard invariant)

## Open questions

- Exact token accounting for “withheld” — approximate OK for v1?
- Does profile (`surgeon` vs `chaos`) set a default mode?

## Out of scope (v1)

- Automatic mode switching mid-step (leave to Model Router experiments later)
