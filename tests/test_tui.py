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

from brain_fakes import is_reaction_call, reaction_for_messages
from relay.bridge import InputState
from relay.config import ModelConfig
from relay.loop import STATUS_COMPLETED
from relay.orchestrator import Event
from relay.transcript import Turn
from relay.tui import (
    ACTOR_BRAIN,
    ACTOR_HANDS,
    ACTOR_YOU,
    RelayTuiApp,
    describe_event_for_activity,
    format_turn,
    present_prompt,
)

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
        if is_reaction_call(system):
            return _resp(reaction_for_messages(messages))
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


def test_describe_event_attributes_brain_hands_you_system():
    """The activity feed maps each emitted event to the right voice."""
    def actor(kind, payload=None):
        return describe_event_for_activity(Event(kind, kind, payload or {}))[0]

    # hands (the executor): its actions, output, questions, step results.
    assert actor("exec_action") == ACTOR_HANDS
    assert actor("executor_question", {"question": "q"}) == ACTOR_HANDS
    assert actor("step_done", {"index": 0, "outcome": "x"}) == ACTOR_HANDS
    assert actor("step_failed", {"index": 0, "reason": "x"}) == ACTOR_HANDS
    # brain (the planner): dispatch, review, answers, escalations, plan moves.
    assert actor("step_start", {"index": 0, "instruction": "x"}) == ACTOR_BRAIN
    assert actor("step_reviewed", {"index": 0, "verdict": "accept"}) == ACTOR_BRAIN
    assert actor("brain_self_answered", {"answer": "a"}) == ACTOR_BRAIN
    assert actor("brain_escalated", {"question": "q"}) == ACTOR_BRAIN
    assert actor("plan_created", {"steps": ["a"]}) == ACTOR_BRAIN
    # you (the human) + system.
    assert actor("user_decided", {"answer": "yes"}) == ACTOR_YOU
    assert actor("status", {"status": "completed"}) is None


# --- the mounted app (headless) -------------------------------------------------


def test_launch_goes_straight_to_an_empty_chat_with_the_model_indicator(tmp_path):
    async def main():
        app = _app(tmp_path, _ArcClient())
        async with app.run_test() as pilot:
            await pilot.pause()
            # Model indicator visible from launch, BEFORE any message.
            assert "vendor/brain" in app._status_text
            assert "vendor/hands" in app._status_text
            assert "AWAITING YOU" in app._status_text  # idle mode word (v0.0.30 status line)
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
            # The activity pane carries the attributed brain<->hands feed; the
            # conversation pane never gets those feed lines.
            assert any(line.startswith("brain | ") for line in app._activity_lines)
            assert any(line.startswith("hands | ") for line in app._activity_lines)
            assert not any(
                line.startswith("brain | ") or line.startswith("hands | ")
                for line in app._conversation_lines
            )
            assert "AWAITING YOU" in app._status_text  # idle mode word (v0.0.30 status line)

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
        if is_reaction_call(system):
            return _resp(reaction_for_messages(messages))
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


class _CountingClient:
    """A client that counts every model call (chat.completions.create)."""

    def __init__(self):
        self.calls = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls += 1
        return _resp("<done>noop</done>")


def test_rendering_events_makes_zero_model_calls(tmp_path):
    """The load-bearing constraint: rendering the activity/conversation from
    already-emitted events adds ZERO model calls / token spend. Feed a proposal +
    an execution event stream straight into the render path and assert the model
    client is never touched -- so a future change can't silently start narrating
    the exchange with a generation."""

    async def main():
        client = _CountingClient()
        app = _app(tmp_path, client)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert client.calls == 0  # nothing has run; the baseline is zero

            events = [
                Event("scope_assessed", "scope", {"scope": "small", "posture": "propose_fast"}),
                Event("plan_proposed", "plan proposed",
                      {"steps": ["create schema", "wire it up"], "assumptions": ["use SQLite"]}),
                Event("step_start", "step 0", {"index": 0, "instruction": "create schema"}),
                Event("exec_action", "edit schema.py", {"kind": "edit", "observation": "wrote schema.py"}),
                Event("executor_question", "do we need OAuth?", {"index": 0, "question": "do we need OAuth?"}),
                Event("brain_self_answered", "answered", {"question": "do we need OAuth?", "answer": "no"}),
                Event("step_reviewed", "review", {"index": 0, "verdict": "accept"}),
                Event("step_done", "done", {"index": 0, "outcome": "schema created"}),
                Event("memory_write", "mem", {"kind": "fact", "summary": "schema created"}),
                Event("status", "all steps complete", {"status": "completed"}),
            ]
            for event in events:
                app._handle_event(event)

            # Rendering the whole brain<->hands stream called the model ZERO times.
            assert client.calls == 0

            # ...and it genuinely rendered: attributed feed + the dual-fidelity split.
            activity = "\n".join(app._activity_lines)
            convo = "\n".join(app._conversation_lines)
            assert any(line.startswith("brain | ") for line in app._activity_lines)
            assert any(line.startswith("hands | ") for line in app._activity_lines)
            assert "brain (assumes): use SQLite" in convo  # assumptions -> conversation
            assert "1. create schema" in activity          # steps -> activity
            assert "create schema" not in convo            # steps stay OUT of conversation

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
