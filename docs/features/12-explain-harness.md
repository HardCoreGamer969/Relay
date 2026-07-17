# 12 — Explain the Harness (`/why`)

**Phase:** A2 · **Status:** [MASTER roadmap table](MASTER.md) only  
**Shipped:** features-revamp (`relay.explain` + `/why` + `relay runs --explain`)  
**Depends on:** existing event stream / transcript (aggregate, don’t reinvent)

## Blockers

- None — must stay zero new model tokens

---

## One-liner

A `/why` flight recorder: why this step was issued, why the brain didn’t re-engage, which budget fired, which assumption blocked auto-answer, what was redacted — exportable per run.

## Locked decisions

| Topic | Decision |
|-------|----------|
| Storage | Recompute from events at finalize; persist compact `harness` dict on RunRecord |
| Raw prompts | Never included |
| Surfaces | TUI `/why`, CLI `relay runs --explain <run_id_prefix>` |

## Acceptance criteria

- [x] `/why` answers without spending new model tokens (deterministic from trace)
- [x] Covers at least: last brain engagement reason, active budgets, open questions
- [x] Export is redacted consistently with `/log` (TUI path runs `redact_secrets`)
- [x] Headless tests assert explanations for scripted runs
