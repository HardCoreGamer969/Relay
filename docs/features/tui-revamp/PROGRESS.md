# TUI Revamp — Progress Journal

Append-only. One section per stage after the review gate passes.

---

## Template

```
### U# — Title (YYYY-MM-DD)

- Status: shipped | blocked
- Commit: <sha>
- Tests: …
- Review: pass / pass-with-notes
- Shipped: …
- Deferred: …
- Next unlocks: …
```

---

## Log

### U3–U6 — Rich stream through polish (2026-07-17)

- Status: shipped (batched)
- Commit: 0716679
- Tests: 946 passed; `tests/test_tui_rich_polish.py`
- Review: pass-with-notes
- Shipped:
  - **U3:** Markdown brain bodies; diff Syntax for observations; tool fold markers `[+]`
  - **U4:** Shift+Enter newlines; `ApproveDialog` (once/session/deny); session allowlist
  - **U5:** `/anim on|off` + `RELAY_TUI_ANIM`; route pulse on `route_change`/phase; kill stops LED
  - **U6:** `relay/assets/logo*.svg`; website palette chrome; `tui.animations` / `tui.plan_dock` prefs; `/find`
- Deferred / blockers:
  - Full RunState session resume — `/runs` still list-only (PROGRESS note)
  - True SVG raster in-terminal — wordmark fallback; assets packaged
  - Click-to-expand tool folds (summary + `/find` path shipped; click widget later)
- Next: maintenance / product features as needed

### U2 — Cockpit chrome (2026-07-17)

- Status: shipped
- Commit: cbd3cba
- Tests: `tests/test_tui_cockpit.py` + full TUI suite
- Review: pass-with-notes
- Shipped:
  - Status rail: phase · step(+instr) · first-class cost (/remaining) · always-on `route=` · ctx window
  - Plan dock (`#plan-dock`) with `/plan full|active|hidden` + narrow coerce; stream gets compact commit line
  - Website palette remap (brain=red, hands=dim); chrome CSS tokens
  - Envelope warn escalates cost slot; `/anim` surface early for U5
- Deferred: Markdown/diffs (U3), approve modal (U4), logo SVG render (U6)
- Next unlocks: U3 rich stream

### U1 — Foundation (2026-07-17)

- Status: shipped
- Commit: 1d74117
- Tests: full suite 928 passed; new `tests/test_tui_foundation.py`
- Review: pass-with-notes
- Shipped:
  - Setup `compose()` starts with empty model Selects; lists load via thread workers
  - `/model` list-provider fetch + `/doctor` probes off UI thread (`run_worker`)
  - Stream/mirrors capped (`STREAM_MAX_LINES` / `STREAM_BUFFER_MAX`); scroll-pin
  - `_marshal` logs + activity note on failure; `Transcript.snapshot_turns` + lock
  - `Command.accepts_args` unified dispatch (`/model`, `/queue`, `/cwd`, …)
  - Structured conversation speaker + exact verdict match
- Deferred / v1 cuts:
  - RichLog / full render-model → U3/U6
  - Sync `persist_role` validate on Enter (dialog contract)
- Next unlocks: U2 cockpit chrome

### U0 — Package split (2026-07-17)

- Status: shipped
- Commit: 844c90d
- Tests: full suite + 164 TUI/CLI/doctor-related
- Review: pass-with-notes
- Shipped:
  - `relay/tui/` package: theme, events, dialogs, setup, input, commands/registry
  - `relay/doctor.py` extracted; CLI/TUI no longer share via private cli imports
  - Public `relay.tui` re-exports unchanged for tests
  - Website brand token stubs in `theme.py` (unused visually)
- Deferred / v1 cuts:
  - Stream/status/controller method bodies still on `RelayTuiApp` (U2)
  - `persist_role` in `setup.py` not yet `config.py`
- Next unlocks: U1 foundation

### Planning lock (2026-07-16)

- Status: designing → planned stages
- Locked: IDE cockpit, full plan dock default, first-class cost, always-on
  route chip, instrument motion + anim kill switch, approve modal, full U0–U6,
  website red/black + SVG logo
- Docs: MASTER, DECISIONS, U0–U6 specs
- Next: U0 package split (no visual change)
