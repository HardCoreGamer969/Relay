"""v0.0.30: the single live STREAM (replacing the two-pane Conversation/Activity).

Headless, network-free. The engine's events are fed straight into the render path
(``_handle_event``) -- the same zero-model-call discipline the rest of the TUI tests
use -- and we assert on the mounted stream + the live-plan state, NOT on animation
frames (the live feel is the maintainer's to verify in a real terminal).

What this pins:
  - there is ONE stream (``#stream``) and the old panes (``#conversation`` /
    ``#activity``) are GONE;
  - brain / hands speakers and findings render with the right cyberpunk color
    (brain = magenta, hands = cyan, findings = green);
  - the plan renders inline as ONE block and a step transitions
    pending -> active -> done IN PLACE (the block is updated, never re-printed);
  - a tool call renders as a compact ``>`` stream line;
  - the status line shows mode (WORKING / AWAITING YOU) and step N/M.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from textual.containers import VerticalScroll

from relay.bridge import RunOutcome
from relay.config import ModelConfig
from relay.orchestrator import Event
from relay.tui import C_CYAN, C_GREEN, C_MAGENTA, RelayTuiApp

CFG = ModelConfig(brain="vendor/brain", hands="vendor/hands")


def _app(tmp_path) -> RelayTuiApp:
    # No run is started; events are fed directly, so a bare client is enough.
    return RelayTuiApp(root=str(tmp_path), models=CFG, client=SimpleNamespace())


def _rows(app):
    """Every rendered stream row as (plain_text, Rich Text|str), in order.

    Reads the app's headless mirror (``_stream_rendered``) rather than Textual widget
    internals, so the assertions are stable across Textual versions."""
    out = []
    for r in app._stream_rendered:
        out.append((r.plain if hasattr(r, "plain") else str(r), r))
    return out


def _has_styled(app, substring, color) -> bool:
    """True if some stream row contains ``substring`` AND carries a span in ``color``."""
    for plain, r in _rows(app):
        if substring in plain and hasattr(r, "spans"):
            if any(color in str(span.style) for span in r.spans):
                return True
    return False


def _stream_text(app) -> str:
    return "\n".join(plain for plain, _ in _rows(app))


# --- the single-stream layout (the two-pane is gone) ---------------------------


def test_one_stream_replaces_the_two_pane(tmp_path):
    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            # The single stream exists...
            assert app.query_one("#stream", VerticalScroll) is not None
            # ...and the old two-pane widgets are gone for good.
            assert len(app.query("#conversation")) == 0
            assert len(app.query("#activity")) == 0

    asyncio.run(main())


# --- speakers + findings render with the cyberpunk palette ---------------------


def test_brain_hands_and_finding_colors(tmp_path):
    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._handle_event(Event("brain_escalated", "escalates", {"question": "ship it?"}))
            app._handle_event(Event("executor_question", "q", {"question": "do we need OAuth?"}))
            app._handle_event(
                Event("hands_finding", "corrupt json", {"finding": "storage.load() eats corrupt json"})
            )
            await pilot.pause()

            # brain = magenta, hands = cyan, findings = green (Relay's split).
            assert _has_styled(app, "escalates", C_MAGENTA)
            assert _has_styled(app, "do we need OAuth?", C_CYAN)
            assert _has_styled(app, "storage.load() eats corrupt json", C_GREEN)
            # the finding is a distinct line, not folded into a tool/plan row.
            assert any("finding" in plain and "storage.load" in plain for plain, _ in _rows(app))

    asyncio.run(main())


# --- the inline live plan updates IN PLACE -------------------------------------


def test_plan_renders_inline_and_a_step_advances_in_place(tmp_path):
    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._handle_event(
                Event("plan_proposed", "plan proposed",
                      {"steps": ["scaffold pkg", "write storage", "wire cli"]})
            )
            await pilot.pause()
            # The plan is ONE inline block (not a per-event re-print).
            assert len(app.query(".plan")) == 1
            assert [s["status"] for s in app._plan_steps] == ["pending", "pending", "pending"]

            app._handle_event(Event("step_start", "step 0", {"index": 0, "instruction": "scaffold pkg"}))
            await pilot.pause()
            assert app._plan_steps[0]["status"] == "active"
            assert app._plan_steps[1]["status"] == "pending"
            assert len(app.query(".plan")) == 1  # SAME block, updated in place

            app._handle_event(Event("step_done", "done", {"index": 0, "outcome": "made it"}))
            app._handle_event(Event("step_start", "step 1", {"index": 1, "instruction": "write storage"}))
            await pilot.pause()
            assert app._plan_steps[0]["status"] == "done"
            assert app._plan_steps[1]["status"] == "active"
            assert len(app.query(".plan")) == 1  # still ONE block, advanced in place

    asyncio.run(main())


# --- tool calls render as compact stream lines ---------------------------------


def test_tool_call_renders_as_a_stream_line(tmp_path):
    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._handle_event(
                Event("exec_action", "read cli.py", {"kind": "read", "observation": "78 lines"})
            )
            await pilot.pause()
            text = _stream_text(app)
            assert "read cli.py" in text
            assert "78 lines" in text
            assert "▸" in text  # the > tool marker rendered inline

    asyncio.run(main())


# --- the status line shows mode + step N/M, and the queue is visible -----------


def test_status_shows_mode_and_step(tmp_path):
    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            # idle -> AWAITING YOU (no plan yet -> no step segment).
            assert "AWAITING YOU" in app._status_text
            assert "step " not in app._status_text

            app._router.begin_run()  # planning -> WORKING
            app._handle_event(
                Event("plan_proposed", "plan proposed", {"steps": ["a", "b"]})
            )
            app._handle_event(Event("step_start", "step 0", {"index": 0, "instruction": "a"}))
            app._update_status()
            assert "WORKING" in app._status_text
            assert "step 1/2" in app._status_text

    asyncio.run(main())


def test_queue_count_is_visible_in_the_status(tmp_path):
    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._session.queue.enqueue("do this next")
            app._session.queue.enqueue("then this")
            app._update_status()
            assert "queued: 2" in app._status_text

    asyncio.run(main())


# --- activity-only motion: the spinner stops when the run ends mid-step ---------


def test_spinner_stops_when_a_run_ends_with_a_step_active(tmp_path):
    """A run that halts mid-step (interrupt/error/...) must NOT leave the active-step
    spinner animating while the engine is idle -- _handle_finished settles the plan."""
    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._router.begin_run()
            app._handle_event(Event("plan_proposed", "plan", {"steps": ["a", "b"]}))
            app._handle_event(Event("step_start", "step 0", {"index": 0, "instruction": "a"}))
            await pilot.pause()
            # mid-step: the spinner is genuinely running.
            assert app._spin_timer is not None
            assert app._plan_steps[0]["status"] == "active"

            # the run ends without that step settling (e.g. an error / interrupt).
            app._handle_finished(RunOutcome(status="error", error="boom"))
            await pilot.pause()
            # no motion continues at the idle prompt, and the step rests (not active).
            assert app._spin_timer is None
            assert app._plan_steps[0]["status"] != "active"

    asyncio.run(main())


# --- the live plan updates in place ACROSS a revision (continued indices) -------


def test_plan_revision_advances_in_place_with_continued_indices(tmp_path):
    """After a replan, settled steps stay and the new pending tail is appended with
    CONTINUED engine indices (orchestrator._adopt_revision); the TUI must keep the
    SAME block and land step_start on the right revised-list position."""
    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._handle_event(Event("plan_proposed", "plan", {"steps": ["a", "b", "c"]}))
            app._handle_event(Event("step_start", "s0", {"index": 0, "instruction": "a"}))
            app._handle_event(Event("step_done", "d0", {"index": 0, "outcome": "ok"}))
            app._handle_event(Event("step_start", "s1", {"index": 1, "instruction": "b"}))
            app._handle_event(Event("step_done", "d1", {"index": 1, "outcome": "ok"}))
            await pilot.pause()
            assert len(app.query(".plan")) == 1

            # The brain revises the remaining tail: only the pending instructions are
            # emitted; the engine continues indexing from the settled count (-> 2, 3).
            app._handle_event(Event("plan_revised", "rev", {"steps": ["c-prime", "d"]}))
            app._handle_event(Event("step_start", "s2", {"index": 2, "instruction": "c-prime"}))
            await pilot.pause()

            statuses = [s["status"] for s in app._plan_steps]
            assert statuses == ["done", "done", "active", "pending"]
            assert app._plan_steps[2]["instruction"] == "c-prime"
            assert len(app.query(".plan")) == 1  # SAME block, advanced in place
            app._update_status()
            assert "step 3/4" in app._status_text

    asyncio.run(main())
