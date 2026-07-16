# 07 — Cost Envelope Contracts

**Phase:** A1 · **Status:** [MASTER roadmap table](MASTER.md) only  
**Depends on:** existing `--max-cost` / step ceilings (extend, don’t reinvent)

## Blockers

- None

---

## One-liner

Before a run, declare a spend/step contract; warn at thresholds; stop with a
partial handoff; end with a brain-vs-hands receipt — CLI and TUI together,
including solo.

## Why it sets Relay apart

Other agents feel open-ended. Relay already bounds loops. Productize budgets as
**contracts with receipts** — the honest-agent brand.

---

## Locked decisions (planning)

| # | Topic | Decision |
|---|--------|----------|
| 1 | Default cost ceiling | **Opt-in.** Steps stay default 50; cost stays off unless flag/env/config sets it. Unbounded cost is stated explicitly in preflight when unset. |
| 2 | Preflight timing | **Start + post-plan snapshot** (see below). |
| 3 | Envelope scope | **Configurable:** `all` (planning + execution) or `execution` (orchestrator/solo loop only). |
| 4 | Breach handoff | **No extra model call in v1.** Deterministic receipt + remaining plan steps / solo state. Spending more to apologize for a spend limit fights the brand. |
| 5 | Warning thresholds | Defaults **50 / 80 / 90 / 99**; user-configurable; **realtime** adjustable in TUI. |
| 6 | “Wasted” metric | **Brain $ on replan/review that did not yield a completed step** (see measurement). |
| 7 | Solo | **Yes** — same envelope applies to `--solo` (easier skill ceiling). |
| 8 | Surfaces | **CLI + TUI together** (preflight, warnings, receipt, `/cost` remaining, wire `max_cost` into bridge). |

### Preflight timing (decision #2 detail)

1. **Run start** — print the contract *before any model spend* (goal panel / TUI status): ceilings, scope, warn thresholds, or `cost: unbounded`.
2. **After plan commit** (planned mode only) — one short line: spent so far / remaining before hands execute. No second full panel.

Solo has only (1).

### Envelope scope (decision #3 detail)

| Value | Behavior |
|-------|----------|
| `all` | Cost (and step warnings where applicable) counted from first model call of the run, including conversational planning / solo turns. Hard stop can fire during planning. |
| `execution` | Planning spend is visible in the post-plan snapshot but **does not** count against the cost ceiling; ceiling checks start when the executor loop starts (today’s orchestrator seam). Solo: entire loop is “execution.” |

**Recommended default when `max_cost` is set:** `all` (honest). When cost is unset, scope is irrelevant for $.

Config sketch (names TBD while implementing):

- CLI: `--envelope-scope all|execution`
- Env: `RELAY_ENVELOPE_SCOPE`
- Config: `envelope.scope` (or top-level `envelope_scope`)
- Precedence: CLI > env > config > default `all`

### Warning thresholds (decision #5 detail)

- Defaults: `0.50, 0.80, 0.90, 0.99` of each *active* dimension (cost and/or step ceiling).
- Fire **once per threshold per dimension per run** at the same boundary seam as the hard stop (after a call/step settles, before the next begins).
- Config: e.g. `RELAY_ENVELOPE_WARN=0.5,0.8,0.9,0.99` or config list; invalid entries fall back to defaults.
- TUI realtime: `/cost` (or `/envelope`) can edit thresholds and ceilings for the **current run**; takes effect on the next boundary check. Does not rewrite user config unless the user explicitly saves (v1: session-only unless we add “save” — see open question).

### Wasted brain $ (decision #6 detail)

Approximate, honest labeling:

- Tag (or classify) brain calls by purpose: `plan` | `replan` | `review` | `answer` | …
- **Wasted brain $** ≈ sum of `replan` + `review` cost for steps that never reached `completed` (failed / blocked / abandoned / cut by envelope).
- If purpose tags aren’t plumbed yet, v1 may start with: brain $ during replan/review phases after a failed step, attributed when the step ends non-completed.
- Receipt label: `wasted brain (replan/review on incomplete steps): $X` — never claim precision we don’t have.

### Hard stop (confirmed)

Keep **finish current step/turn, then halt** — no mid-`call_model` abort in v1. Applies to planned + solo.

### Breach output (no LLM handoff)

- Terminal status unchanged in spirit: `max_cost` / `max_steps` (solo may mirror with existing `max_steps` / new cost status).
- Print/show: receipt + **remaining plan steps** (planned) or “stopped before `<done>`” (solo).
- Friendly how-to-raise copy stays.

---

## User surface (v1)

### CLI

- Existing: `--max-cost`, `--max-total-steps`, env/config
- New: `--envelope-scope`, warn-threshold override (flag and/or env)
- Preflight in the run panel; post-plan remaining line
- Boundary warnings as stream lines (once each)
- Receipt under/beside current telemetry table

### TUI

- Wire `resolve_max_cost()` (+ scope, thresholds) into bridge/runner (parity with CLI)
- Status / `/cost`: spent, remaining, ceilings, scope, next threshold
- Warning lines in the activity stream
- Realtime adjust thresholds (and ceilings) for the in-flight run
- Same receipt at goal end

### Solo

- Cost ceiling checked in `run_task` on a turn boundary (same idea as planned step boundary)
- Step ceiling remains `--max-steps` for solo; envelope receipt shows both where set

---

## Hooks into existing code

- `resolve_max_cost` / `resolve_max_total_steps` (`config.py`) — extend with scope + warn list resolvers
- Orchestrator step-boundary cost check (`orchestrator.py`) — add warnings; honor scope `all` via planning-path checks
- Solo loop (`loop.py`) — cost ceiling + warnings
- Bridge (`bridge.py`) — pass `max_cost`, scope, thresholds (today steps only)
- CLI preflight panel + `_print_telemetry` → receipt (`cli.py`)
- TUI `/cost`, status segment (`tui.py`)
- Telemetry (`telemetry.py`) — optional call purpose tag for wasted-$; ledger helpers for remaining / fraction
- Runlog (`runlog.py`) — persist envelope + receipt fields when cheap

---

## Acceptance criteria

- [ ] Preflight declares envelope (or explicit `cost: unbounded`) before model spend; headless/TTY_COMPATIBLE safe
- [ ] Post-plan snapshot shows spent/remaining when planned + cost set
- [ ] Scope `all` vs `execution` behavior covered by tests
- [ ] Warnings at 50/80/90/99 fire once each per active dimension; custom threshold lists work
- [ ] Hard stop still finishes current step/turn; statuses + non-zero exit where CLI already does
- [ ] Receipt: brain vs hands $, wasted brain $ (defined above), $/completed-step when computable, envelope outcome
- [ ] Solo respects `max_cost` + warnings
- [ ] TUI: `max_cost` wired; `/cost` shows remaining; realtime threshold edit affects next check
- [ ] Hermetic tests; no live network

---

## Out of scope (v1)

- Billing / team wallets
- Mid-call abort of `call_model`
- LLM “what I’d do next” handoff on breach
- Assumption profiles setting default envelopes (B1)
- Counterfactual “$ saved vs premium” (router / bake-off later)
- Persisting TUI realtime envelope edits to disk (unless we affirmatively add save — see below)

---

## Open questions (narrow)

1. **TUI realtime edits — session-only or “save to config”?**  
   Recommendation: **session-only in v1** (simple, no foot-guns); document that config/env still set the next run’s defaults.

2. **Scope default when only step ceiling is set (cost unbounded)?**  
   Warnings still apply to the step dimension; scope only affects whether *planning* model calls count toward **cost**. Steps in planned mode are already executor-scoped. Confirm: scope knob is **cost-scoped only** (name it that way in help text).

3. **Naming:** `/cost` vs new `/envelope` for the richer panel?  
   Recommendation: keep **`/cost`** as the command (users know it); panel title “Envelope” when ceilings exist.

---

## Implementation slices (still no code until you say go)

| Slice | Deliverable |
|-------|-------------|
| S1 | Resolvers: scope + warn thresholds; preflight string helper; unit tests |
| S2 | Planned path: warnings + receipt; scope `execution` preserves today’s cost seam; scope `all` checks during planning |
| S3 | Solo path: cost ceiling + warnings + receipt fields |
| S4 | TUI/bridge wire-up + `/cost` remaining + realtime threshold/ceiling session edits |
| S5 | Runlog fields + polish copy; update this doc checkboxes + MASTER status when shipped |

Suggested PR shape: one PR for S1–S3 (engine + CLI), follow-up PR for S4–S5 if the TUI diff gets large — or one PR if you prefer a single A1 land. **Your call before coding.**
