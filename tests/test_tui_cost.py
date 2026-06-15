"""v0.0.20: the two-tier live cost counter (per-goal + session). Headless, no network.

Relay SHOWS spend and lets the user stop -- there is no cap. Two figures:
  - ``_goal_cost``: the current goal's live cost; reset on a new goal, but kept showing
    the last goal's total while idle (never blinks to $0 on finish).
  - ``_session_cost``: cumulative over FINISHED goals; folded at finish, untouched by a
    new goal.
The whole cost path reads an already-tracked figure off the run's ledger -- zero model
calls (asserted in the animation test, part 2).
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from textual.widgets import Input

from relay.bridge import RunOutcome
from relay.config import ModelConfig
from relay.tui import RelayTuiApp

CFG = ModelConfig(brain="vendor/brain", hands="vendor/hands")


def _app(tmp_path, *, client=None):
    return RelayTuiApp(
        root=str(tmp_path), models=CFG, client=client or SimpleNamespace(),
        list_models_fn=lambda provider, **k: [],
        validate_fn=lambda provider, model: (True, "ok"),
    )


def _fake_runner(cost):
    """A stand-in runner exposing just what the cost/finish path reads."""
    return SimpleNamespace(
        ledger=SimpleNamespace(total_cost=lambda: cost),
        transcript=SimpleNamespace(turns=[]),
    )


def _completed():
    return RunOutcome(status="completed", result=object())


def _resp_nocost(content):
    usage = SimpleNamespace(prompt_tokens=6, completion_tokens=4, total_tokens=10, cost=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))], usage=usage)


class _NoCostParking:
    """A fake brain that scopes + proposes (so the worker parks at the reaction ask),
    reporting NO cost -- so the per-goal counter stays at its reset value during the
    test (lets us assert the reset cleanly without a cost race)."""

    def __init__(self):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, *, model, messages, **kwargs):
        if model == "vendor/hands":
            return _resp_nocost("<done>noop</done>")
        system = " ".join(messages[0]["content"].split())
        if "assess the goal's SCOPE" in system:
            return _resp_nocost("<scope>small</scope><reason>self-contained</reason>")
        if "precise, executor-ready plan" in system:
            return _resp_nocost("<plan><step>do the thing</step></plan><headline>A thing.</headline>")
        return _resp_nocost("<verdict>accept</verdict>")


async def _until(pilot, predicate, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await pilot.pause(0.02)
    return predicate()


async def _submit(pilot, app, text):
    app.query_one("#prompt", Input).value = text
    await pilot.press("enter")


async def _teardown(pilot, app):
    runner = app._runner
    if runner is not None and runner.is_running:
        runner.cancel()
        await _until(pilot, lambda: not runner.is_running)


# --- fold at finish + idle display ------------------------------------------


def test_fold_at_finish_and_idle_shows_last_goal(tmp_path):
    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            # goal 1 in flight: the live counter tracks the ledger.
            app._router.begin_run()
            app._runner = _fake_runner(0.0123)
            app._refresh_cost()
            assert app._goal_cost == 0.0123
            assert app._session_total() == 0.0123  # in-flight goal counted live

            app._handle_finished(_completed())
            assert app._session_cost == 0.0123      # folded at finish
            assert app._goal_cost == 0.0123         # idle: still the last goal's total
            assert app._session_total() == 0.0123   # no double count once idle

            # goal 2 (state-level new goal): folds on top, doesn't clear the session.
            app._goal_cost = 0.0
            app._router.begin_run()
            app._runner = _fake_runner(0.02)
            app._refresh_cost()
            app._handle_finished(_completed())
            assert round(app._session_cost, 6) == 0.0323  # 0.0123 + 0.02

    asyncio.run(main())


def test_new_goal_resets_goal_cost_but_not_session(tmp_path):
    """Starting a real run via submit zeroes the per-goal counter; the session total is
    left untouched (the no-cost parking client keeps the reset observable)."""
    async def main():
        app = _app(tmp_path, client=_NoCostParking())
        async with app.run_test() as pilot:
            await pilot.pause()
            app._goal_cost = 0.05    # pretend a prior goal left a total
            app._session_cost = 0.07
            await _submit(pilot, app, "fresh goal")
            await pilot.pause()
            assert app._goal_cost == 0.0      # _start_run zeroed it
            assert app._session_cost == 0.07  # session untouched by a new goal
            await _teardown(pilot, app)

    asyncio.run(main())


# --- the status-line counter ------------------------------------------------


def test_status_counter_renders_affordance_and_toggle(tmp_path):
    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._goal_cost = 0.0234
            app._update_status()
            assert "$0.0234" in app._status_text
            assert "esc to stop" not in app._status_text  # idle: no stop affordance

            app._router.begin_run()  # -> in flight
            app._update_status()
            assert "$0.0234" in app._status_text
            assert "esc to stop" in app._status_text  # active: affordance shows

            app._cost_visible = False  # the /cost toggle hides it
            app._update_status()
            assert "$0.0234" not in app._status_text
            app._cost_visible = True
            app._update_status()
            assert "$0.0234" in app._status_text

    asyncio.run(main())
