"""Headless tests for the reusable SegmentedControl primitive (no network).

Logic-level assertions on the testable core (move with wrap, highlighted_value,
select_highlighted, Esc cancels) -- not pixel checks. SegmentedControl is a
general component; /provider's role step is just its first consumer.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from relay.config import ModelConfig
from relay.tui import RelayTuiApp, SegmentedControl

CFG = ModelConfig(brain="vendor/brain", hands="vendor/hands")

OPTIONS = [
    {"label": "brain", "value": "brain"},
    {"label": "hands", "value": "hands"},
    {"label": "both", "value": "both"},
]


def _app(tmp_path):
    return RelayTuiApp(root=str(tmp_path), models=CFG, client=SimpleNamespace())


def _push(app, **kw):
    seg = SegmentedControl(title="Pick", options=OPTIONS, **kw)
    app.push_screen(seg)
    return seg


def test_start_index_and_highlighted_value(tmp_path):
    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            seg = _push(app, start_index=0)
            await pilot.pause()
            assert seg.highlighted_value() == "brain"
            seg2 = SegmentedControl(title="t", options=OPTIONS, start_index=1)
            assert seg2.highlighted_value() == "hands"

    asyncio.run(main())


def test_move_wraps_at_both_ends(tmp_path):
    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            seg = _push(app, start_index=0)
            await pilot.pause()
            assert seg.highlighted_value() == "brain"
            seg.move(1); assert seg.highlighted_value() == "hands"
            seg.move(1); assert seg.highlighted_value() == "both"
            seg.move(1); assert seg.highlighted_value() == "brain"   # wrap forward
            seg.move(-1); assert seg.highlighted_value() == "both"   # wrap backward

    asyncio.run(main())


def test_select_highlighted_commits_and_calls_on_select(tmp_path):
    chosen = {}

    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            seg = _push(app, start_index=0, on_select=lambda v: chosen.update(v=v))
            await pilot.pause()
            seg.move(2)  # -> "both"
            seg.select_highlighted()
            await pilot.pause()
            assert chosen == {"v": "both"}
            assert not isinstance(app.screen, SegmentedControl)  # dismissed

    asyncio.run(main())


def test_escape_cancels_without_selecting(tmp_path):
    chosen = {}

    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            seg = _push(app, start_index=0, on_select=lambda v: chosen.update(v=v))
            await pilot.pause()
            seg.action_close()  # Esc path
            await pilot.pause()
            assert chosen == {}  # on_select never fired
            assert not isinstance(app.screen, SegmentedControl)

    asyncio.run(main())


def test_left_right_key_bindings_move_highlight(tmp_path):
    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            seg = _push(app, start_index=0)
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            assert seg.highlighted_value() == "hands"
            await pilot.press("left")
            await pilot.press("left")  # wrap past brain -> both
            await pilot.pause()
            assert seg.highlighted_value() == "both"

    asyncio.run(main())
