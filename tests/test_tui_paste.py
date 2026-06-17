"""Headless tests for multi-line paste capture in the prompt (v0.0.25, Part 3).

Textual's single-line ``Input`` keeps only the first line of a paste
(``event.text.splitlines()[0]``), silently dropping the rest -- so a pasted
multi-line goal/spec was corrupted to its first line. ``PromptInput`` overrides
the paste handler to capture the WHOLE paste; typed Enter-to-submit is unchanged.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from textual import events

from relay.config import ModelConfig
from relay.tui import RelayTuiApp

CFG = ModelConfig(brain="vendor/brain", hands="vendor/hands")
WAIT_S = 8.0

MULTILINE = "Build a CLI that:\n- adds tasks\n- lists tasks\n- stores them in JSON"


def _app(tmp_path) -> RelayTuiApp:
    # A client is needed only so the app considers itself configured; no run starts
    # in these tests (we assert on the prompt's captured value / submit path).
    return RelayTuiApp(root=str(tmp_path), models=CFG, client=SimpleNamespace())


async def _until(pilot, predicate, timeout=WAIT_S):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await pilot.pause(0.02)
    return predicate()


def test_multiline_paste_is_captured_in_full(tmp_path):
    async def main():
        from textual.widgets import Input

        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            prompt = app.query_one("#prompt", Input)
            prompt.focus()
            prompt.post_message(events.Paste(MULTILINE))
            assert await _until(pilot, lambda: prompt.value == MULTILINE)
            # The WHOLE paste is present -- not just the first line.
            assert prompt.value == MULTILINE
            assert "lists tasks" in prompt.value
            assert "stores them in JSON" in prompt.value

    asyncio.run(main())


def test_paste_does_not_submit_on_its_own(tmp_path):
    """A newline within a paste is content, not a submit: pasting must not start a
    run -- submission stays an explicit Enter."""

    async def main():
        from textual.widgets import Input

        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            prompt = app.query_one("#prompt", Input)
            prompt.focus()
            prompt.post_message(events.Paste(MULTILINE))
            assert await _until(pilot, lambda: prompt.value == MULTILINE)
            # Paste alone did not submit: no run started, still on the welcome view.
            assert app._runner is None
            assert app._view == "welcome"

    asyncio.run(main())


def test_typed_line_then_enter_still_submits(tmp_path):
    """Enter-to-submit for TYPED input is unchanged: typing a line and pressing
    Enter starts a run and clears the box."""

    async def main():
        from textual.widgets import Input

        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            prompt = app.query_one("#prompt", Input)
            prompt.value = "build a snake game"
            await pilot.press("enter")
            assert await _until(pilot, lambda: app._runner is not None)
            # The submit consumed the text (box cleared) and started a run.
            assert prompt.value == ""
            assert app._view == "working"

    asyncio.run(main())
