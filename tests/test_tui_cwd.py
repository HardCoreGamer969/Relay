"""Headless TUI tests for the session-sticky working directory (v0.0.25, Part 2).

The working directory is a SESSION concept: once established it persists across
subsequent goals (the next goal operates from it, not the launch root), and a
working-dir change that lived only in a cancelled/never-executed plan does not
silently take effect. These drive the real app via Textual's headless harness.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

from brain_fakes import is_reaction_call, reaction_for_messages
from relay.bridge import InputState
from relay.config import ModelConfig
from relay.tui import RelayTuiApp

CFG = ModelConfig(brain="vendor/brain", hands="vendor/hands")
WAIT_S = 8.0


def _resp(content):
    usage = SimpleNamespace(prompt_tokens=6, completion_tokens=4, total_tokens=10, cost=0.0)
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))], usage=usage)


class _Client:
    """A fake brain+hands: scope -> propose -> reaction -> accept."""

    def __init__(self, hands=()):
        self._hands = list(hands)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, *, model, messages, **kwargs):
        if model == "vendor/hands":
            return _resp(self._hands.pop(0))
        system = " ".join(messages[0]["content"].split())
        if "assess the goal's SCOPE" in system:
            return _resp("<scope>small</scope><reason>x</reason>")
        if "precise, executor-ready plan" in system:
            return _resp("<plan><step>do the thing</step></plan><headline>A thing.</headline>")
        if is_reaction_call(system):
            return _resp(reaction_for_messages(messages))
        return _resp("<verdict>accept</verdict>")


def _app(tmp_path, client) -> RelayTuiApp:
    return RelayTuiApp(root=str(tmp_path), models=CFG, client=client)


async def _until(pilot, predicate, timeout=WAIT_S):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await pilot.pause(0.02)
    return predicate()


async def _submit(pilot, app, text):
    from textual.widgets import Input

    app.query_one("#prompt", Input).value = text
    await pilot.press("enter")


def test_established_working_dir_is_used_for_the_next_goal(tmp_path):
    """Establishing a working dir persists it: the next run's root is the sticky
    subfolder, not the launch root."""
    sub = tmp_path / "lunar_lander_testing"
    sub.mkdir()

    async def main():
        app = _app(tmp_path, _Client())
        async with app.run_test() as pilot:
            await pilot.pause()
            ok, note = app._set_working_dir(str(sub))  # the user establishes it
            assert ok, note
            assert app._session.working_dir == sub.resolve()

            await _submit(pilot, app, "build the lander")
            assert await _until(pilot, lambda: app._runner is not None)
            # The new run's root reflects the sticky dir -- NOT the launch root.
            assert Path(app._runner.project_root) == sub.resolve()
            assert Path(app._runner.project_root) != tmp_path.resolve()
            # ...and it is surfaced to the user in the status line.
            assert "cwd=lunar_lander_testing" in app._status_text

    asyncio.run(main())


def test_cancelled_plan_does_not_change_the_working_dir(tmp_path):
    """A plan the user cancels (declines) leaves the working dir untouched -- a cwd
    change that was only in a never-executed plan does not silently persist."""

    async def main():
        app = _app(tmp_path, _Client())
        async with app.run_test() as pilot:
            await _submit(pilot, app, "build it in some subfolder")
            assert await _until(
                pilot, lambda: app._router.state is InputState.AWAITING_REACTION
            )
            # Reject the plan: the brain reads "cancel" as reject -> declined run.
            await _submit(pilot, app, "cancel")
            assert await _until(pilot, lambda: app._router.state is InputState.IDLE)
            # The working dir never moved off the launch root.
            assert app._session.is_launch_root()
            assert app._session.working_dir == tmp_path.resolve()

    asyncio.run(main())


def test_set_working_dir_rejects_a_nonexistent_directory(tmp_path):
    async def main():
        app = _app(tmp_path, _Client())
        async with app.run_test() as pilot:
            await pilot.pause()
            ok, note = app._set_working_dir(str(tmp_path / "nope"))
            assert not ok and "not an existing directory" in note
            assert app._session.is_launch_root()  # unchanged on rejection

    asyncio.run(main())
