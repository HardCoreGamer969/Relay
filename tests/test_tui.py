"""Headless tests for the Textual TUI (no live terminal; Textual's run_test).

The app is mounted with Textual's headless test harness and driven through the
real Input widget; the engine underneath is the real worker-thread runner
against a routed fake client (no network). Assertions read the app's
render-path buffers (``_conversation_lines`` / ``_status_text``) -- exactly the
strings handed to the widgets -- so the tests pin the render path itself,
including its unicode-cleanliness.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from relay.bridge import InputState
from relay.config import ModelConfig
from relay.loop import STATUS_COMPLETED
from relay.transcript import Turn
from relay.tui import RelayTuiApp, format_turn, present_prompt

CFG = ModelConfig(brain="vendor/brain", hands="vendor/hands")
WAIT_S = 8.0

# Real unicode that the legacy console path used to mangle: em-dash, arrow, check.
UNICODE_HEADLINE = "Login — simple → done ✓"


def _resp(content):
    usage = SimpleNamespace(prompt_tokens=6, completion_tokens=4, total_tokens=10, cost=0.00002)
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))], usage=usage)


class _ArcClient:
    """A fake brain+hands routed by model, then by brain system prompt."""

    def __init__(self, *, hands=(), headline=UNICODE_HEADLINE):
        self._hands = list(hands)
        self._headline = headline
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, *, model, messages, **kwargs):
        self.calls.append({"model": model, "messages": messages, **kwargs})
        if model == "vendor/hands":
            assert self._hands, "ran out of hands replies"
            return _resp(self._hands.pop(0))
        system = " ".join(messages[0]["content"].split())
        if "assess the goal's SCOPE" in system:
            return _resp("<scope>small</scope><reason>self-contained</reason>")
        if "precise, executor-ready plan" in system:
            return _resp(f"<plan><step>add login</step></plan><headline>{self._headline}</headline>")
        if "asked a question mid-step" in system:
            return _resp("<decision>escalate</decision>"
                         "<ask_user>Should login support OAuth?</ask_user>")
        if "readable narrative" in system.lower():
            return _resp("Earlier, you asked for login and committed.")
        return _resp("<verdict>accept</verdict>")


def _app(tmp_path, client) -> RelayTuiApp:
    return RelayTuiApp(root=str(tmp_path), models=CFG, client=client)


async def _until(pilot, predicate, timeout=WAIT_S):
    """Yield to the app loop until ``predicate`` is true (or time out)."""
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


# --- pure render-path pieces ---------------------------------------------------


def test_present_prompt_is_a_passthrough_chokepoint():
    text = "Should the login be simple — OAuth → Google only?"
    assert present_prompt(text) == text  # full fidelity, unchanged (v1 contract)


def test_format_turn_preserves_real_unicode():
    turn = Turn(id="t1", speaker="brain", phase="proposal",
                text="em—dash, arrow → and a check ✓", created_at=1)
    rendered = format_turn(turn)
    assert "em—dash" in rendered and "→" in rendered and "✓" in rendered
    assert rendered == "brain (proposal): em—dash, arrow → and a check ✓"


def test_format_turn_labels_speakers_plainly():
    assert format_turn(Turn(id="t1", speaker="user", phase="reaction",
                            text="ok", created_at=1)) == "you (reaction): ok"


# --- the mounted app (headless) -------------------------------------------------


def test_launch_goes_straight_to_an_empty_chat_with_the_model_indicator(tmp_path):
    async def main():
        app = _app(tmp_path, _ArcClient())
        async with app.run_test() as pilot:
            await pilot.pause()
            # Model indicator visible from launch, BEFORE any message.
            assert "vendor/brain" in app._status_text
            assert "vendor/hands" in app._status_text
            assert "[idle]" in app._status_text
            assert app._router.state is InputState.IDLE
            assert app._runner is None  # no landing screen, no auto-started run
            assert app._conversation_lines == []

    asyncio.run(main())


def test_full_loop_in_the_tui_one_box_one_thread(tmp_path):
    """Goal -> proposal -> 'ok' -> execution -> escalation in the SAME box -> done."""

    async def main():
        client = _ArcClient(hands=[
            "<question>do we need OAuth login?</question>",
            '<edit path="login.py">x</edit>\n<done>added login</done>',
        ])
        app = _app(tmp_path, client)
        async with app.run_test() as pilot:
            await _submit(pilot, app, "add login")
            assert await _until(
                pilot, lambda: app._router.state is InputState.AWAITING_REACTION
            )
            # Dual-fidelity split: the conversation pane shows the human-readable
            # headline (a transcript turn), NOT the numbered executor plan; the
            # full numbered plan lives in the activity pane.
            convo = "\n".join(app._conversation_lines)
            activity = "\n".join(app._activity_lines)
            assert UNICODE_HEADLINE in convo       # unicode survived, un-sanitized
            assert "1. add login" not in convo     # numbered steps stay OUT of convo
            assert "1. add login" in activity      # ...they render in the activity pane

            await _submit(pilot, app, "ok")  # commit
            assert await _until(
                pilot, lambda: app._router.state is InputState.AWAITING_DECISION
            )
            joined = "\n".join(app._conversation_lines)
            assert "Should login support OAuth?" in joined  # escalation = next turn

            await _submit(pilot, app, "yes, Google OAuth")  # same box, same thread
            assert await _until(pilot, lambda: app._runner.outcome is not None)
            assert await _until(pilot, lambda: app._router.state is InputState.IDLE)

            assert app._runner.outcome.status == STATUS_COMPLETED
            assert (tmp_path / "login.py").read_text(encoding="utf-8") == "x"
            joined = "\n".join(app._conversation_lines)
            assert "you (goal): add login" in joined
            assert "you (decision): yes, Google OAuth" in joined
            assert "(result)" in joined  # the closing result turn rendered
            # The activity pane got the firehose; the conversation did not.
            assert any("step_start" in line for line in app._activity_lines)
            assert not any("step_start" in line for line in app._conversation_lines)
            assert "[idle]" in app._status_text

    asyncio.run(main())


class _AssumeClient:
    """A brain that proposes 2 steps + 2 surfaced assumptions (with a headline)."""

    def __init__(self, *, hands):
        self._hands = list(hands)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, *, model, messages, **kwargs):
        if model == "vendor/hands":
            return _resp(self._hands.pop(0))
        system = " ".join(messages[0]["content"].split())
        if "assess the goal's SCOPE" in system:
            return _resp("<scope>small</scope><reason>self-contained</reason>")
        if "precise, executor-ready plan" in system:
            return _resp(
                "<plan><step>create schema</step><step>wire it up</step></plan>"
                "<headline>Build the store.</headline>"
                "<assume>use SQLite</assume><assume>no auth</assume>"
            )
        if "readable narrative" in system.lower():
            return _resp("Earlier you committed a small plan.")
        return _resp("<verdict>accept</verdict>")


def test_proposal_split_conversation_is_headline_plus_assumptions_only(tmp_path):
    """Conversation pane = headline + surfaced assumptions; the numbered executor
    steps render in the activity pane. Holds pre-commit AND in scroll-back."""

    async def main():
        client = _AssumeClient(hands=["<done>schema made</done>", "<done>wired</done>"])
        app = _app(tmp_path, client)
        async with app.run_test() as pilot:
            await _submit(pilot, app, "build a store")
            assert await _until(
                pilot, lambda: app._router.state is InputState.AWAITING_REACTION
            )

            # -- pre-commit: the split holds while the user is reacting --
            convo = "\n".join(app._conversation_lines)
            activity = "\n".join(app._activity_lines)
            assert "Build the store." in convo                 # the headline (transcript)
            assert "brain (assumes): use SQLite" in convo      # surfaced assumptions
            assert "brain (assumes): no auth" in convo
            assert "create schema" not in convo                # NO executor steps in convo
            assert "wire it up" not in convo
            assert "1. create schema" in activity              # ...steps live in activity
            assert "2. wire it up" in activity

            await _submit(pilot, app, "ok")
            assert await _until(pilot, lambda: app._runner.outcome is not None)
            assert await _until(pilot, lambda: app._router.state is InputState.IDLE)

            # -- scroll-back: the conversation pane is STILL the clean story --
            convo = "\n".join(app._conversation_lines)
            assert "Build the store." in convo
            assert "brain (assumes): use SQLite" in convo
            assert "create schema" not in convo  # the executor plan never leaked in
            assert "wire it up" not in convo

    asyncio.run(main())


def test_submit_while_busy_is_ignored_not_misrouted(tmp_path):
    class _SlowScope(_ArcClient):
        def _create(self, *, model, messages, **kwargs):
            time.sleep(0.3)  # hold the engine in "planning" long enough to type
            return super()._create(model=model, messages=messages, **kwargs)

    async def main():
        app = _app(tmp_path, _SlowScope())
        async with app.run_test() as pilot:
            await _submit(pilot, app, "add login")
            assert await _until(pilot, lambda: app._router.state is InputState.PLANNING)
            await _submit(pilot, app, "impatient typing")
            assert any("input ignored" in line for line in app._activity_lines)
            # ...and the impatient text never became a conversation turn.
            assert not any("impatient typing" in line for line in app._conversation_lines)
            app._runner.cancel()
            assert await _until(pilot, lambda: not app._runner.is_running)

    asyncio.run(main())


def test_quit_cancels_and_joins_the_worker_no_orphan(tmp_path):
    """The cost-leak guard: quitting never leaves a worker calling the API."""

    async def main():
        app = _app(tmp_path, _ArcClient())
        async with app.run_test() as pilot:
            await _submit(pilot, app, "add login")
            # Park the engine mid-conversation (blocked in user_turn).
            assert await _until(
                pilot, lambda: app._router.state is InputState.AWAITING_REACTION
            )
            runner = app._runner
            assert runner.is_running
            await app.action_quit()
            # By the time quit returns, the worker is cancelled AND joined.
            assert not runner.is_running
            assert runner.join(0.1) is True

    asyncio.run(main())


def test_escape_cancels_the_run_cleanly(tmp_path):
    async def main():
        client = _ArcClient(hands=['<edit path="a.txt">A</edit>\n<done>made a.txt</done>'])
        app = _app(tmp_path, client)
        async with app.run_test() as pilot:
            await _submit(pilot, app, "add login")
            assert await _until(
                pilot, lambda: app._router.state is InputState.AWAITING_REACTION
            )
            await pilot.press("escape")  # cancel while parked: unblocks the ask
            assert await _until(pilot, lambda: app._runner.outcome is not None)
            assert app._runner.outcome.status == "cancelled"
            assert await _until(pilot, lambda: not app._runner.is_running)
            assert any("cancel" in line for line in app._activity_lines)

    asyncio.run(main())
