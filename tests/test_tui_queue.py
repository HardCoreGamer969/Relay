"""v0.0.28 Part 3 (TUI): /queue defers + FIFO-consumes, up-arrow recalls, queued count.

Driven through the real headless app. Planning always asks the user to react to the
plan (the bridge parks on user_turn), so each run is committed by delivering an "ok"
reaction; on completion a queued item is consumed next. Network-free.
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


class _Client:
    """Scopes/proposes (parking at the reaction ask), then the hands completes. Records
    each goal the planner saw, so tests can assert consumption order."""

    def __init__(self):
        self.goals: list[str] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, *, model, messages, **kwargs):
        if model == "vendor/hands":
            return _resp("<done>done</done>")
        system = " ".join(messages[0]["content"].split())
        if "assess the goal's SCOPE" in system:
            for line in messages[-1]["content"].splitlines():
                if line.startswith("Goal:"):
                    self.goals.append(line[len("Goal:"):].strip())
                    break
            return _resp("<scope>small</scope><reason>x</reason>")
        if "precise, executor-ready plan" in system:
            return _resp("<plan><step>do it</step></plan><headline>A thing.</headline>")
        if is_reaction_call(system):
            return _resp(reaction_for_messages(messages))
        return _resp("<verdict>accept</verdict>")


def _app(tmp_path):
    return RelayTuiApp(root=str(tmp_path), models=CFG, client=_Client())


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


async def _await_reaction(pilot, app):
    return await _until(pilot, lambda: app._router.state is InputState.AWAITING_REACTION)


def test_queue_defers_then_consumes_fifo(tmp_path):
    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await _submit(pilot, app, "first goal")
            assert await _await_reaction(pilot, app)
            # Queue two items WHILE the first run is in flight: no interruption.
            app._do_queue("task two")
            app._do_queue("task three")
            assert app._session.queue.pending() == ["task two", "task three"]

            # Commit the first; it completes and the queue consumes "task two" next.
            await _submit(pilot, app, "ok")
            assert await _until(pilot, lambda: "task two" in app._client.goals)
            assert await _await_reaction(pilot, app)
            await _submit(pilot, app, "ok")  # commit task two -> consume task three
            assert await _until(pilot, lambda: "task three" in app._client.goals)
            assert await _await_reaction(pilot, app)
            await _submit(pilot, app, "ok")  # commit task three -> queue drained
            assert await _until(
                pilot, lambda: not app._session.queue and app._router.state is InputState.IDLE
            )
            # FIFO order preserved across the whole session.
            assert app._client.goals == ["first goal", "task two", "task three"]

    asyncio.run(main())


def test_queue_does_not_interrupt_the_current_run(tmp_path):
    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await _submit(pilot, app, "the goal")
            assert await _await_reaction(pilot, app)
            app._do_queue("next up")
            # Queuing never interrupts: no cancel was requested, run still in flight.
            assert app._interrupting is False
            assert app._router.state is InputState.AWAITING_REACTION
            assert app._session.queue.pending() == ["next up"]

    asyncio.run(main())


def test_status_line_shows_queued_count(tmp_path):
    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await _submit(pilot, app, "goal")
            assert await _await_reaction(pilot, app)
            app._do_queue("a")
            app._do_queue("b")
            app._update_status()
            assert "queued: 2" in app._status_text

    asyncio.run(main())


def test_up_arrow_recalls_recent_input_including_queued(tmp_path):
    async def main():
        from textual.widgets import Input

        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await _submit(pilot, app, "build the app")
            assert await _await_reaction(pilot, app)
            app._do_queue("queued task")  # recorded to history too

            prompt = app.query_one("#prompt", Input)
            prompt.focus()
            await pilot.press("up")  # most recent first: the queued item
            assert prompt.value == "queued task"
            await pilot.press("up")  # older
            assert prompt.value == "build the app"

    asyncio.run(main())


def test_recalled_item_can_be_edited_and_requeued(tmp_path):
    async def main():
        from textual.widgets import Input

        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await _submit(pilot, app, "goal one")
            assert await _await_reaction(pilot, app)
            app._do_queue("rough task")

            prompt = app.query_one("#prompt", Input)
            prompt.focus()
            await pilot.press("up")  # recalls "rough task"
            assert prompt.value == "rough task"
            prompt.value = "/queue rough task refined"  # edit + re-queue inline
            await pilot.press("enter")
            assert app._session.queue.pending()[-1] == "rough task refined"
            assert app._session.history.items()[-1] == "rough task refined"

    asyncio.run(main())
