# U0 — Package Split

**Shipped:** features-revamp (`relay/tui/` package + `relay/doctor.py`)

**Stage:** U0 · **Status:** [MASTER](MASTER.md) only  
**Maps to:** REVAMP Stage T1  
**Depends on:** nothing (enabler)

## One-liner

Split `relay/tui.py` (~3k lines) into `relay/tui/` with **zero intentional
behavior change** — the safety net for every later stage.

## Target layout

```
relay/tui/
  __init__.py      # re-export RelayTuiApp
  app.py           # compose, lifecycle, marshal, quit
  theme.py         # palette + CSS variables (website tokens stubbed)
  stream.py        # stream writers + in-place plan block + mirrors
  events.py        # describe_event_for_activity (share with cli)
  status.py        # status line + placeholders
  controller.py    # run lifecycle / interrupt / queue / cost fold
  input.py         # PromptInput + slash popover
  dialogs.py       # SelectDialog, TextEntryDialog, SegmentedControl
  setup.py         # SetupScreen
  commands/        # registry + config/ops command modules
```

## Also move out of the view

- `persist_role` → `relay/config.py` (or thin wrapper)
- Doctor helpers → `relay/doctor.py` (kill tui→cli private import)
- Key resolution helpers out of the view file

## Acceptance

- [x] `from relay.tui import RelayTuiApp` still works
- [x] All existing `tests/test_tui*.py` green with import-path updates only
- [x] No intentional rendered-output diffs (mirror buffers assertable)
- [x] `bridge.py` untouched

## v1 cuts

- No visual redesign in U0 (theme file may define website tokens unused yet)
- Stream / status / controller **methods** remain on `RelayTuiApp` (stub modules
  document deferral to U2); widgets + pure helpers are extracted.
- `persist_role` lives in `setup.py` (not yet `config.py`)
