"""Headless tests for the TUI welcome state + handoff (no live terminal).

The visual *feel* (glitch frames, layout) is the user's to verify in a real
terminal -- these tests pin only what's deterministic and real: the pure
render-path pieces (wordmark, greetings, model identity, glitch resolve) and
the STATE TRANSITIONS via Textual's headless harness (launch in welcome state;
first goal hands off to the working state and kicks the worker). We assert
state + content, never animation frames.
"""

from __future__ import annotations

import asyncio
import random
import time
from types import SimpleNamespace

from brain_fakes import is_reaction_call, reaction_for_messages
from relay.bridge import InputState
from relay.config import ModelConfig
from relay.tui import (
    GREETINGS,
    INPUT_PLACEHOLDERS,
    RELAY_WORDMARK,
    RelayTuiApp,
    glitch_frame,
    model_identity,
    pick_greeting,
    pick_placeholder,
    placeholder_for_state,
    _normalize_block,
    _glitch_thresholds,
)

CFG = ModelConfig(brain="vendor/brain", hands="vendor/hands")
WAIT_S = 8.0


def _resp(content):
    usage = SimpleNamespace(prompt_tokens=6, completion_tokens=4, total_tokens=10, cost=0.00002)
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))], usage=usage)


class _ParkingClient:
    """A fake brain that scopes + proposes, so the worker parks at user_turn."""

    def __init__(self):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, *, model, messages, **kwargs):
        if model == "vendor/hands":
            return _resp("<done>noop</done>")
        system = " ".join(messages[0]["content"].split())
        if "assess the goal's SCOPE" in system:
            return _resp("<scope>small</scope><reason>self-contained</reason>")
        if "precise, executor-ready plan" in system:
            return _resp("<plan><step>do the thing</step></plan><headline>A small thing.</headline>")
        if is_reaction_call(system):
            return _resp(reaction_for_messages(messages))
        return _resp("<verdict>accept</verdict>")


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


async def _teardown(pilot, app):
    """Unblock + join any parked worker so no thread lingers between tests."""
    runner = app._runner
    if runner is not None and runner.is_running:
        runner.cancel()
        await _until(pilot, lambda: not runner.is_running)


# --- pure render-path pieces ---------------------------------------------------


def test_greetings_non_empty_and_pick_is_a_member():
    assert GREETINGS
    assert all(isinstance(g, str) and g for g in GREETINGS)
    for _ in range(20):
        assert pick_greeting() in GREETINGS


def test_model_identity_reflects_the_configured_pairing():
    identity = model_identity(CFG)
    assert "vendor/brain" in identity and "vendor/hands" in identity
    assert "brain" in identity and "hands" in identity


def test_input_placeholders_rotate_and_do_not_echo_the_greeting():
    # A small rotating set, same voice as the greetings...
    assert len(INPUT_PLACEHOLDERS) >= 4
    assert all(isinstance(p, str) and p for p in INPUT_PLACEHOLDERS)
    for _ in range(20):
        assert pick_placeholder() in INPUT_PLACEHOLDERS
    # ...but DISJOINT from GREETINGS, so the box never echoes the greeting above it.
    assert set(INPUT_PLACEHOLDERS).isdisjoint(set(GREETINGS))


def test_placeholder_is_state_aware():
    idle = "Describe the goal..."
    # Awaiting states each get their own short cue.
    assert placeholder_for_state(InputState.AWAITING_REACTION, idle) != idle
    assert "approve" in placeholder_for_state(InputState.AWAITING_REACTION, idle)
    assert placeholder_for_state(InputState.AWAITING_DECISION, idle) == "Your answer..."
    assert "y/n" in placeholder_for_state(InputState.AWAITING_APPROVAL, idle)
    # Idle (and welcome) falls back to the per-launch rotating phrase.
    assert placeholder_for_state(InputState.IDLE, idle) == idle


def test_launch_uses_a_rotating_idle_placeholder(tmp_path):
    async def main():
        app = RelayTuiApp(root=str(tmp_path), models=CFG, client=_ParkingClient())
        async with app.run_test() as pilot:
            await pilot.pause()
            from textual.widgets import Input

            assert app._placeholder in INPUT_PLACEHOLDERS
            # The real Input widget shows that rotating idle phrase at launch.
            assert app.query_one("#prompt", Input).placeholder == app._placeholder
            await _teardown(pilot, app)

    asyncio.run(main())


def test_placeholder_follows_engine_state(tmp_path):
    async def main():
        app = RelayTuiApp(root=str(tmp_path), models=CFG, client=_ParkingClient())
        async with app.run_test() as pilot:
            from textual.widgets import Input

            await _submit(pilot, app, "build a thing")
            assert await _until(
                pilot, lambda: app._router.state is InputState.AWAITING_REACTION
            )
            # The box now reflects what a submit means: reacting to the plan.
            placeholder = app.query_one("#prompt", Input).placeholder
            assert placeholder == placeholder_for_state(
                InputState.AWAITING_REACTION, app._placeholder
            )
            assert "approve" in placeholder
            await _teardown(pilot, app)

    asyncio.run(main())


def test_wordmark_is_a_clean_rectangular_block():
    lines = _normalize_block(RELAY_WORDMARK)
    assert len(lines) == 5
    assert len({len(line) for line in lines}) == 1  # uniform width
    assert "█" in RELAY_WORDMARK  # real block glyphs, rendered unicode


def test_glitch_frame_resolves_to_the_target_at_the_ends():
    lines = _normalize_block(RELAY_WORDMARK)
    target = "\n".join(lines)
    thresholds = _glitch_thresholds(lines)
    shimmer = random.Random(0)
    # "in" is fully resolved at progress 1; scrambled (differs) at progress 0.
    assert glitch_frame(lines, thresholds, 1.0, shimmer, direction="in") == target
    assert glitch_frame(lines, thresholds, 0.0, shimmer, direction="in") != target
    # "out" is the target at progress 0; scrambled by progress 1.
    assert glitch_frame(lines, thresholds, 0.0, shimmer, direction="out") == target
    assert glitch_frame(lines, thresholds, 1.0, shimmer, direction="out") != target


# --- the welcome state (headless) ----------------------------------------------


def test_launches_in_the_welcome_state_not_the_working_panes(tmp_path):
    async def main():
        app = RelayTuiApp(root=str(tmp_path), models=CFG, client=_ParkingClient())
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._view == "welcome"
            # The welcome screen is shown; the working panes are NOT.
            assert app.query_one("#welcome").display is True
            assert app.query_one("#working").display is False
            assert app._runner is None  # no run, no worker
            assert app._conversation_lines == []
            # Identity is promoted on the welcome screen, before any message.
            assert app._greeting in GREETINGS
            assert "vendor/brain" in app._indicator_text
            assert "vendor/hands" in app._indicator_text

    asyncio.run(main())


def test_first_goal_transitions_to_working_and_kicks_the_worker(tmp_path):
    async def main():
        app = RelayTuiApp(root=str(tmp_path), models=CFG, client=_ParkingClient())
        async with app.run_test() as pilot:
            await pilot.pause()
            await _submit(pilot, app, "build a thing")

            # The worker is kicked off immediately (not gated on the animation),
            # and the logical view is already "working".
            assert app._runner is not None
            assert app._view == "working"
            assert "you (goal): build a thing" in app._conversation_lines

            # The datamosh resolves and the working panes are revealed.
            assert await _until(pilot, lambda: app.query_one("#working").display is True)
            assert app.query_one("#welcome").display is False
            # The engine really ran: it reached the reaction ask in the same box.
            assert await _until(pilot, lambda: app._router.state is InputState.AWAITING_REACTION)

            await _teardown(pilot, app)

    asyncio.run(main())


def test_off_mode_reveals_the_working_panes_instantly(tmp_path):
    """The "off" mode hook: no timers, the handoff is synchronous."""

    async def main():
        app = RelayTuiApp(root=str(tmp_path), models=CFG, client=_ParkingClient(), anim_mode="off")
        async with app.run_test() as pilot:
            await pilot.pause()
            # off-mode startup resolves the wordmark instantly (no timer pending).
            assert app._anim_timer is None
            await _submit(pilot, app, "go")
            # The transition's on_done ran synchronously inside the submit.
            assert app.query_one("#working").display is True
            assert app.query_one("#welcome").display is False
            assert app._anim_timer is None

            await _teardown(pilot, app)

    asyncio.run(main())


def test_welcome_state_does_not_return_after_a_run_starts(tmp_path):
    """Welcome is a launch-only state -- once working, it stays working."""

    async def main():
        app = RelayTuiApp(root=str(tmp_path), models=CFG, client=_ParkingClient(), anim_mode="off")
        async with app.run_test() as pilot:
            await pilot.pause()
            await _submit(pilot, app, "first goal")
            assert app._view == "working"
            assert app.query_one("#welcome").display is False
            await _teardown(pilot, app)
            # Even after the run is torn down, the welcome screen is gone for good.
            assert app.query_one("#welcome").display is False
            assert app.query_one("#working").display is True

    asyncio.run(main())
