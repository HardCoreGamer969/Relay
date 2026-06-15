"""Headless tests for the operational slash commands: /doctor /runs /assume /clear.

Network-touching work (doctor preflight, runs reading) is behind injected seams,
so these stay network-free. /clear must never clobber an in-flight run.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from relay.config import ModelConfig
from relay.tui import RelayTuiApp, SelectDialog, visible_commands

CFG = ModelConfig(brain="vendor/brain", hands="vendor/hands")


def _app(tmp_path, **kw):
    return RelayTuiApp(root=str(tmp_path), models=CFG, client=SimpleNamespace(), **kw)


# --- /doctor (reuses the preflight via an injected seam) --------------------


def test_doctor_renders_preflight_rows(tmp_path):
    rows = [
        {"role": "brain", "provider": "openrouter", "model": "anthropic/x",
         "status": "OK", "note": "resolved"},
        {"role": "hands", "provider": "deepseek", "model": "deepseek-v4-flash",
         "status": "FAILED", "note": "no endpoints"},
    ]

    async def main():
        app = _app(tmp_path, doctor_fn=lambda: rows)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._cmd_doctor()
            await pilot.pause()
            dialog = app.screen
            assert isinstance(dialog, SelectDialog)
            blob = "\n".join(o["title"] for o in dialog._options)
            assert "brain" in blob and "OK" in blob
            assert "hands" in blob and "FAILED" in blob

    asyncio.run(main())


# --- /runs (reuses the runlog reader via an injected seam) ------------------


def test_runs_lists_records(tmp_path):
    records = [
        SimpleNamespace(run_id="abcd1234ef", status="completed",
                        roles={"brain": "vendor/brain"}, totals={"cost_usd": 0.0012}),
        SimpleNamespace(run_id=" deadbeef99", status="declined",
                        roles={"hands": "deepseek-v4-flash"}, totals={}),
    ]

    async def main():
        app = _app(tmp_path, runs_fn=lambda: records)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._cmd_runs()
            await pilot.pause()
            dialog = app.screen
            assert isinstance(dialog, SelectDialog)
            titles = "\n".join(o["title"] for o in dialog._options)
            assert "completed" in titles and "declined" in titles

    asyncio.run(main())


def test_runs_empty_is_graceful(tmp_path):
    async def main():
        app = _app(tmp_path, runs_fn=lambda: [])
        async with app.run_test() as pilot:
            await pilot.pause()
            app._cmd_runs()
            await pilot.pause()
            titles = "\n".join(o["title"] for o in app.screen._options)
            assert "no runs" in titles.lower()

    asyncio.run(main())


# --- /assume (a select, not an inline number) -------------------------------


def test_assume_sets_session_level(tmp_path):
    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._assumption_level == "auto"
            app._cmd_assume()
            await pilot.pause()
            dialog = app.screen
            assert isinstance(dialog, SelectDialog)
            assert "3" in dialog.visible_values() and "auto" in dialog.visible_values()
            dialog.choose("3")
            await pilot.pause()
            assert app._assumption_level == "3"   # applied to the session

    asyncio.run(main())


# --- /clear (panes only; never clobbers an in-flight run) -------------------


def test_clear_empties_panes(tmp_path):
    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._write_conversation("you (goal): build x")
            app._write_activity("brain | plan: 1 step", actor=None)
            assert app._conversation_lines and app._activity_lines
            app._cmd_clear()
            assert app._conversation_lines == []
            assert app._activity_lines == []

    asyncio.run(main())


def test_clear_is_disabled_and_inert_mid_run(tmp_path):
    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._write_conversation("you (goal): build x")
            # Simulate an in-flight run.
            app._runner = SimpleNamespace(is_running=True)
            # Hidden from the popover...
            assert "clear" not in {c.name for c in visible_commands(app)}
            # ...and inert even if invoked directly (the run is never clobbered).
            app._cmd_clear()
            assert app._conversation_lines == ["you (goal): build x"]

    asyncio.run(main())
