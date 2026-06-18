"""v0.0.28 Part 1 (TUI): esc interrupts -> interrupt prompt -> stop preserves session.

Driven through the real headless app: a goal starts, the worker parks at the
reaction ask, esc interrupts (clean cancel), the app lands in the INTERRUPTED state
with the session intact, and a stop (esc again) returns to IDLE without tearing the
session down. Network-free.
"""

from __future__ import annotations

import asyncio
import time
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


class _Parking:
    """Scopes + proposes, so the worker parks at the reaction ask (mid-planning)."""

    def __init__(self):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, *, model, messages, **kwargs):
        if model == "vendor/hands":
            return _resp("<done>noop</done>")
        system = " ".join(messages[0]["content"].split())
        if "assess the goal's SCOPE" in system:
            return _resp("<scope>small</scope><reason>self-contained</reason>")
        if "precise, executor-ready plan" in system:
            return _resp("<plan><step>do the thing</step></plan><headline>A thing.</headline>")
        if is_reaction_call(system):
            return _resp(reaction_for_messages(messages))
        return _resp("<verdict>accept</verdict>")


def _app(tmp_path) -> RelayTuiApp:
    return RelayTuiApp(root=str(tmp_path), models=CFG, client=_Parking())


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


def test_esc_interrupts_into_the_interrupt_prompt_session_intact(tmp_path):
    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await _submit(pilot, app, "build a thing")
            assert await _until(pilot, lambda: app._runner is not None and app._runner.is_running)
            convo_before = list(app._conversation_lines)

            app.action_cancel_run()  # esc: interrupt
            # The run halts cleanly and the app lands in the INTERRUPTED state.
            assert await _until(pilot, lambda: app._router.state is InputState.INTERRUPTED)
            assert "interrupted" in app._status_text.lower()
            # Session preserved: the conversation so far is still there; cwd unchanged.
            assert app._conversation_lines[: len(convo_before)] == convo_before
            assert app._session.is_launch_root()
            # The worker joined cleanly (money-leak guard intact).
            assert await _until(pilot, lambda: not app._runner.is_running)

    asyncio.run(main())


def test_stop_from_interrupt_preserves_session_and_returns_to_idle(tmp_path):
    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await _submit(pilot, app, "build a thing")
            assert await _until(pilot, lambda: app._runner is not None and app._runner.is_running)
            app.action_cancel_run()  # esc: interrupt
            assert await _until(pilot, lambda: app._router.state is InputState.INTERRUPTED)

            convo = list(app._conversation_lines)
            app.action_cancel_run()  # esc again: STOP
            # Stop returns to IDLE but keeps the session (conversation/cwd intact).
            assert app._router.state is InputState.IDLE
            assert app._conversation_lines[: len(convo)] == convo
            assert app._session.transcript is not None  # session not torn down
            assert any("session preserved" in line.lower() for line in app._activity_lines)

    asyncio.run(main())


def test_empty_submit_at_interrupt_is_stop(tmp_path):
    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await _submit(pilot, app, "build a thing")
            assert await _until(pilot, lambda: app._runner is not None and app._runner.is_running)
            app.action_cancel_run()
            assert await _until(pilot, lambda: app._router.state is InputState.INTERRUPTED)
            await _submit(pilot, app, "")  # empty submit -> STOP
            assert app._router.state is InputState.IDLE

    asyncio.run(main())
