# 12 — Explain the Harness (`/why`)

**Phase:** A2 · **Status:** planned · **Depends on:** existing event stream / transcript (aggregate, don’t reinvent)

[← Master plan](MASTER.md)

---

## One-liner

A `/why` flight recorder: why this step was issued, why the brain didn’t re-engage, which budget fired, which assumption blocked auto-answer, what was redacted — exportable per run.

## Why it sets Relay apart

Agents are black boxes. Relay’s orchestrator is already explicit. Sell **debuggable autonomy**, especially for bake-offs and router decisions later.

## User surface

- TUI: `/why` [optional step id] → structured explanation from run events
- CLI: `relay runs explain <id>` 
- Export bundle alongside redacted `/log`
- Machine-readable JSON for tests and duel postmortems

## Hooks into existing code

- Transcript / event descriptions (`describe_event_for_activity` pattern)
- Orchestrator decision points (replan, abort, repeated_step, escalation)
- Assumption dial + (later) firewall class + router choice records
- `debug.py` redaction for safe export

## Acceptance criteria

- [ ] `/why` answers without spending new model tokens (deterministic from trace)
- [ ] Covers at least: last brain engagement reason, active budgets, open questions
- [ ] Export is redacted consistently with `/log`
- [ ] Headless tests assert explanations for scripted runs

## Open questions

- Store explanations eagerly vs recompute from event log?
- How much raw prompt text is ever shown (privacy / size)?

## Out of scope (v1)

- Natural-language LLM summary of the trace (optional later; would spend tokens)
