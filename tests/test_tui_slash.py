"""Headless tests for the TUI slash-command system (Textual run_test; no network).

Part 1 here: the command registry, the ``/`` popover, and the two generic dialogs
(SelectDialog / TextEntryDialog). Network-touching seams are injected so nothing
hits the network. The load-bearing rule across all slash tests: commands are
dialog-driven with ZERO inline arguments, and no key is ever read from the prompt.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from textual.widgets import Input

from relay.config import ModelConfig
from relay.tui import (
    COMMANDS,
    Command,
    RelayTuiApp,
    SelectDialog,
    TextEntryDialog,
    filter_commands,
    visible_commands,
)

CFG = ModelConfig(brain="vendor/brain", hands="vendor/hands")


def _app(tmp_path, **kw):
    # Injected client => "configured" (no first-run auto-open); seams keep
    # listing/validation/doctor/runs off the network.
    return RelayTuiApp(
        root=str(tmp_path), models=CFG, client=SimpleNamespace(),
        list_models_fn=lambda provider, **k: ["deepseek-v4-flash", "deepseek-v4-pro"],
        validate_fn=lambda provider, model: (True, "ok"),
        doctor_fn=lambda: [{"role": "brain", "provider": "openrouter",
                            "model": "m", "status": "OK", "note": "resolved"}],
        runs_fn=lambda: [],
        **kw,
    )


# --- the registry (pure, no mount) ------------------------------------------


def test_registry_is_data_records():
    assert COMMANDS, "registry must not be empty"
    names = [c.name for c in COMMANDS]
    assert len(names) == len(set(names)), "command names must be unique"
    for c in COMMANDS:
        assert isinstance(c, Command)
        assert c.name and c.title and c.description and c.category
        assert callable(c.run)
    # The headline commands exist.
    assert {"help", "model", "key", "config", "doctor", "runs", "assume", "clear"} <= set(names)


def test_visible_commands_respects_enabled_predicate():
    idle = SimpleNamespace(_runner=None)
    running = SimpleNamespace(_runner=SimpleNamespace(is_running=True))
    assert "clear" in {c.name for c in visible_commands(idle)}
    # /clear is enabled=not run-active, so it's hidden while a run is in flight.
    assert "clear" not in {c.name for c in visible_commands(running)}


def test_filter_commands_narrows_by_text():
    app = SimpleNamespace(_runner=None)
    assert [c.name for c in filter_commands(app, "mod")] == ["model"]
    assert {c.name for c in filter_commands(app, "")} == {c.name for c in COMMANDS}
    assert filter_commands(app, "zzz-nope") == []


# --- the / popover (mounted) ------------------------------------------------


def test_typing_slash_opens_and_filters_popover(tmp_path):
    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#prompt", Input).value = "/"
            await pilot.pause()
            assert app._popover_open is True
            assert {c.name for c in app._popover_commands} == {c.name for c in visible_commands(app)}
            assert app.query_one("#command-popover").display is True

            app.query_one("#prompt", Input).value = "/mod"
            await pilot.pause()
            assert [c.name for c in app._popover_commands] == ["model"]

    asyncio.run(main())


def test_non_slash_goal_does_not_open_popover(tmp_path):
    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#prompt", Input).value = "build a thing"
            await pilot.pause()
            assert app._popover_open is False

    asyncio.run(main())


def test_enter_runs_highlighted_command(tmp_path):
    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#prompt", Input).value = "/help"
            await pilot.pause()
            assert app._popover_open is True
            await pilot.press("enter")
            await pilot.pause()
            # /help opened the generic select dialog, and the popover closed.
            assert isinstance(app.screen, SelectDialog)
            assert app._popover_open is False

    asyncio.run(main())


def test_escape_closes_popover(tmp_path):
    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#prompt", Input).value = "/"
            await pilot.pause()
            assert app._popover_open is True
            await pilot.press("escape")
            await pilot.pause()
            assert app._popover_open is False

    asyncio.run(main())


def test_clearing_slash_dismisses_popover(tmp_path):
    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#prompt", Input).value = "/mo"
            await pilot.pause()
            assert app._popover_open is True
            app.query_one("#prompt", Input).value = ""  # deleted the slash
            await pilot.pause()
            assert app._popover_open is False

    asyncio.run(main())


# --- the generic select dialog ----------------------------------------------


def test_select_dialog_filters_and_selects(tmp_path):
    chosen = {}

    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            options = [
                {"title": "Alpha", "value": "a", "on_select": lambda v: chosen.update(v=v)},
                {"title": "Beta", "value": "b", "on_select": lambda v: chosen.update(v=v)},
                {"title": "Gamma", "value": "g", "on_select": lambda v: chosen.update(v=v)},
            ]
            app.push_screen(SelectDialog(title="Pick", options=options))
            await pilot.pause()
            dialog = app.screen
            assert dialog.visible_values() == ["a", "b", "g"]
            dialog.apply_filter("bet")
            assert dialog.visible_values() == ["b"]
            dialog.apply_filter("")
            dialog.move(1)  # highlight Beta
            dialog.select_highlighted()
            await pilot.pause()
            assert chosen == {"v": "b"}

    asyncio.run(main())


# --- the generic text-entry dialog ------------------------------------------


def test_text_entry_dialog_masked_and_submits(tmp_path):
    captured = {}

    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(TextEntryDialog(
                title="Secret", label="enter:", password=True,
                on_submit=lambda v: (captured.update(v=v) or (True, "ok")),
            ))
            await pilot.pause()
            dialog = app.screen
            assert dialog.query_one("#entry-input", Input).password is True  # masked
            dialog.query_one("#entry-input", Input).value = "typed-value"
            assert dialog.submit() is True
            assert captured == {"v": "typed-value"}

    asyncio.run(main())


def test_text_entry_dialog_stays_open_on_failure(tmp_path):
    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(TextEntryDialog(
                title="X", label="enter:", on_submit=lambda v: (False, "rejected: bad"),
            ))
            await pilot.pause()
            dialog = app.screen
            dialog.query_one("#entry-input", Input).value = "whatever"
            assert dialog.submit() is False
            assert "rejected" in dialog.status_text

    asyncio.run(main())
