"""U2 cockpit: status rail (cost/route) + plan dock modes."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from relay.config import ModelConfig
from relay.envelope import CostEnvelope
from relay.orchestrator import Event
from relay.tui import RelayTuiApp
from relay.tui.plan import resolve_plan_mode, visible_plan_indices
from relay.tui.status import cost_segment, route_segment

CFG = ModelConfig(brain="vendor/brain", hands="vendor/hands")


def _app(tmp_path, **kw):
    return RelayTuiApp(
        root=str(tmp_path), models=CFG, client=SimpleNamespace(),
        list_models_fn=lambda provider, **k: [],
        validate_fn=lambda provider, model: (True, "ok"),
        anim_mode="off",
        **kw,
    )


def test_resolve_plan_mode_narrow_coerces_full():
    assert resolve_plan_mode("full", width=80, pinned_full=False) == "active"
    assert resolve_plan_mode("full", width=80, pinned_full=True) == "full"
    assert resolve_plan_mode("hidden", width=40) == "hidden"


def test_visible_plan_indices_active_window():
    steps = [
        {"instruction": "a", "status": "done"},
        {"instruction": "b", "status": "active"},
        {"instruction": "c", "status": "pending"},
        {"instruction": "d", "status": "pending"},
    ]
    assert visible_plan_indices(steps, "full") == [0, 1, 2, 3]
    assert visible_plan_indices(steps, "active") == [0, 1, 2]
    assert visible_plan_indices(steps, "hidden") == []


def test_cost_segment_with_envelope_remaining():
    env = CostEnvelope(max_cost=1.0)
    ledger = SimpleNamespace(total_cost=lambda: 0.25)
    # chargeable_cost uses ledger; stub methods via real envelope
    text, level = cost_segment(
        goal_cost=0.25, visible=True, run_in_flight=False, stopping=False,
        envelope=env, ledger=ledger,
    )
    assert "/ $" in text and "left" in text
    assert level == "normal"


def test_route_segment_always_present():
    assert route_segment(None) == "route=balanced"
    router = SimpleNamespace(contract=SimpleNamespace(name="economy"), bumps_frozen=True)
    assert route_segment(router) == "route=economy*"


def test_status_rail_shows_route_and_cost(tmp_path):
    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._goal_cost = 0.12
            app._update_status()
            assert "route=" in app._status_text
            assert "$0.1200" in app._status_text or "$0.12" in app._status_text

    asyncio.run(main())


def test_plan_dock_modes_switch(tmp_path):
    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._show_working()
            app._handle_event(Event("plan_proposed", "p", {"steps": ["a", "b", "c"]}))
            app._handle_event(Event("step_start", "s", {"index": 1, "instruction": "b"}))
            await pilot.pause()
            dock = app.query_one("#plan-dock")
            assert app._plan_steps[1]["status"] == "active"
            app._cmd_plan("hidden")
            await pilot.pause()
            assert "-hidden" in dock.classes
            app._cmd_plan("full")
            await pilot.pause()
            assert "-hidden" not in dock.classes

    asyncio.run(main())


def test_envelope_warn_escalates_cost_level(tmp_path):
    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            env = CostEnvelope(max_cost=1.0, warn_thresholds=(0.5, 0.8))
            ledger = SimpleNamespace(total_cost=lambda: 0.6)
            app._runner = SimpleNamespace(envelope=env, ledger=ledger)
            app._refresh_cost()
            assert app._cost_warn_level == "warn"
            snap = app._status_snapshot()
            assert "left" in snap.cost
            assert snap.cost_level in ("warn", "pulse")

    asyncio.run(main())
