# Router v2 — Progress Journal

Append-only review log. One section per feature after the review gate passes.
Do not delete history; mark corrections with a new dated note.

---

## Template (copy per feature)

```
### E# — Title (YYYY-MM-DD)

- Status: shipped | blocked
- Commit: <sha>
- Tests: <count or files>
- Review: pass / pass-with-notes
- Shipped:
  - …
- Deferred / v1 cuts:
  - …
- Next unlocks: …
```

---

## Log

### E1 — Route contracts (2026-07-16)

- Status: shipped
- Tests: `tests/test_router_v2.py` (E1 block) + v1 `tests/test_router.py`
- Review: pass
- Shipped:
  - `RouteContract` schema v2; `parse` / `load` / `save` of `.relay/route.json`
  - Precedence: CLI > repo contract > env > config > default
  - Contract pins; unknown keys retained for doctor later
  - `relay route show|set`
- Deferred / v1 cuts:
  - No signing / cloud sync; doctor warn on unknown keys later
- Next unlocks: E2 call-class

### E2 — Call-class routing (2026-07-16)

- Status: shipped
- Tests: E2 block in `tests/test_router_v2.py`
- Review: pass
- Shipped:
  - `CALL_CLASSES` + default purpose→model map on contracts
  - `ModelRouter.models_for_purpose` emits `route_change` with `purpose`
  - Orchestrator hands path tags `purpose=hands_step` and remaps via call-class
- Deferred / v1 cuts:
  - No free-text ML classifier; harness tags only
- Next unlocks: E3, E8, E9

### E3 — Envelope-as-UX (2026-07-16)

- Status: shipped
- Tests: `test_e3_broker_line_*`
- Review: pass
- Shipped:
  - `format_broker_line` for CLI preflight + planned-run panel
  - TUI `/route` cockpit (route, pins, freeze, phase)
- Deferred / v1 cuts:
  - No sparkline; text-first
- Next unlocks: E4

### E4 — Counterfactual receipt (2026-07-16)

- Status: shipped
- Tests: `test_e4_counterfactual_*`
- Review: pass
- Shipped:
  - `estimate_counterfactual_cost` (catalog or fixed approx rates × tokens)
  - CLI receipt lines + `--counterfactual` (default premium; skipped when already on baseline)
- Deferred / v1 cuts:
  - Catalog miss → fallback rates still labeled approx; no dual live calls
- Next unlocks: E5

### E5 — Explain the spend (2026-07-16)

- Status: shipped
- Tests: `test_e5_explain_spend_*`
- Review: pass
- Shipped:
  - `explain_spend` markdown from `route_change` + ledger purpose buckets
  - Embedded in `HarnessReport.spend` / `/why` text; `relay runs --explain` surfaces it
- Deferred / v1 cuts:
  - No interactive chart
- Next unlocks: E6

### E6 — Cheap skeptic assassin (2026-07-16)

- Status: shipped
- Tests: `test_e6_*`
- Review: pass
- Shipped:
  - Default skeptic call-class = economy/hands slug under route
  - `review_plan_adversarially(..., model_router=)` remaps before investigate
  - `RELAY_SKEPTIC_MODEL` pin
- Deferred / v1 cuts:
  - No auto-escalate skeptic on findings
- Next unlocks: E7

### E7 — Provider micro-routing (2026-07-16)

- Status: shipped
- Tests: `test_e7_*`
- Review: pass
- Shipped:
  - Contract `provider_sort` / `max_price`
  - `call_model(..., route_contract=)` attaches OpenRouter extras + optional `:floor`
  - Non-OpenRouter no-op
- Deferred / v1 cuts:
  - No BYOK economics UI
- Next unlocks: E8

### E8 — Phase-aware routes (2026-07-16)

- Status: shipped
- Tests: `test_e8_*`
- Review: pass
- Shipped:
  - Contract `phases.planning|execution|review`
  - `ModelRouter.set_phase`; orchestrator sets planning then execution
  - Phase role overrides beat static call-class map
- Deferred / v1 cuts:
  - Diff-accept maps to review when present (not separate phase UI)
- Next unlocks: E9, E10

### E9 — Fitness-gated hands (2026-07-16)

- Status: shipped
- Tests: `test_e9_*`
- Review: pass
- Shipped:
  - Parse-failure threshold → `fitness_bump`; clean streak → `fitness_decay`
  - Envelope freeze still blocks bumps
  - Wired in `_run_executor_step`
- Deferred / v1 cuts:
  - No mid-run `relay probe`; parse_failures as proxy
- Next unlocks: E10

### E10 — Orchestra × router (2026-07-16)

- Status: shipped
- Tests: `test_e10_*`
- Review: pass
- Shipped:
  - Orchestra workers pass `model_router`; `hands-N` uses `hands_step` call-class
  - Broker line notes `orchestra=N×hands_step`
- Deferred / v1 cuts:
  - No per-worker model diversity beyond hands call-class
- Next unlocks: E11

### E11 — Repo-learned route (2026-07-16)

- Status: shipped
- Tests: `test_e11_*`
- Review: pass
- Shipped:
  - `recommend_route` from `.relay/duels/` lowest completed cost, else runlog
  - `relay route recommend [--apply]`
- Deferred / v1 cuts:
  - Heuristic only; never silent apply
- Next unlocks: E12

### E12 — Shadow routing (2026-07-16)

- Status: shipped
- Tests: `test_e12_*`
- Review: pass
- Shipped:
  - `RELAY_SHADOW_ROUTE` / `--shadow-route` log-only to `.relay/shadow.jsonl`
  - Dual-call remains off by default (`shadow.dual_call`)
- Deferred / v1 cuts:
  - No quality scoring of shadow choices; dual-call path not exercised live
- Next unlocks: (phase E complete)
