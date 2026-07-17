# 04 — Hands Context Dial

**Phase:** B3 · **Status:** [MASTER roadmap table](MASTER.md) only  
**Shipped:** features-revamp (`resolve_hands_context_mode` + orchestrator prompt modes)  
**Depends on:** A2 helpful for proving “narrow wins”; pairs with B4 router

## Blockers

- None

---

## One-liner

Expose executor narrowness as a user control and show how much context the hands *didn’t* see — amnesia as a feature.

## Why it sets Relay apart

Everyone races to stuff more context in. Relay’s moat is **less** context for hands. Make it visible and tunable.

## Dial levels (v1)

| Level | Hands sees |
|-------|------------|
| `needle` | Current step + one-line carry-over (**default**) |
| `findings` | Above + shared findings/directives |
| `summary` | Above + compact summaries of prior steps |
| `wide` | Debug mode: more prior hands/decision transcript (expensive, distractible) |

**Hard invariant:** even `wide` never receives the brain’s private reasoning pool.

## User surface

- `RELAY_HANDS_CONTEXT_MODE` / `--hands-context needle|findings|summary|wide`
- Help text documents that `wide` is for debugging, not recommended default
- (TUI status / tokens-withheld estimate deferred)

## Hooks into existing code

- Narrow executor prompt assembly in `orchestrator.py` (`_executor_step_prompt`)
- `MemoryBus` shared/hands pools (`memory.py`)
- Telemetry for tokens per role (later)

## Acceptance criteria

- [x] Four modes change hands prompt contents in tests (substring)
- [x] Default remains `needle` (preserve architecture promise)
- [x] Status/help documents that `wide` is for debugging, not recommended default
- [x] No hands access to full brain reasoning even in `wide` (hard invariant)

## Open questions

- Exact token accounting for “withheld” — approximate OK for v1?
- Does profile (`surgeon` vs `chaos`) set a default mode? **v1: no.**

## Out of scope (v1)

- Automatic mode switching mid-step (leave to Model Router experiments later)
- TUI “tokens withheld” estimate
