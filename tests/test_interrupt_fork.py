"""v0.0.28 Part 1: esc = interrupt (session preserved) + the steer/stop fork.

esc halts the RUN at the clean boundary (v0.0.20 cancel seam, unchanged) and lands
the user at an interrupt prompt; the SESSION (conversation, cwd, memory, cost) is
never torn down. From the interrupt prompt: typed input -> STEER (redirect now),
empty/esc-again -> STOP (abandon the plan, keep the session). These tests pin the
router fork and the session-preservation primitives headlessly (no Textual).
"""

from __future__ import annotations

from relay.bridge import (
    ACTION_ANSWER,
    ACTION_START,
    ACTION_STEER,
    ACTION_STOP,
    InputQueue,
    InputRouter,
    InputState,
    Session,
    UiRequest,
    REQUEST_REACTION,
)


# --- the InputRouter interrupt fork -----------------------------------------


def test_interrupt_enters_interrupted_state():
    router = InputRouter()
    router.begin_run()
    router.interrupt()
    assert router.state is InputState.INTERRUPTED
    assert router.interrupted is True


def test_interrupted_typed_input_is_steer():
    router = InputRouter()
    router.begin_run()
    router.interrupt()
    outcome = router.submit("use Postgres instead")
    assert outcome.action == ACTION_STEER
    assert outcome.text == "use Postgres instead"
    # A steer kicks off a continuation run, so the router is busy again.
    assert router.state is InputState.PLANNING


def test_interrupted_empty_input_is_stop():
    router = InputRouter()
    router.begin_run()
    router.interrupt()
    outcome = router.submit("   ")
    assert outcome.action == ACTION_STOP
    # Stop returns to a clean IDLE -- the session lives on, but no run is active.
    assert router.state is InputState.IDLE


def test_interrupt_does_not_affect_normal_idle_submit():
    router = InputRouter()
    # A normal idle submit is unchanged by the new fork.
    assert router.submit("build a thing").action == ACTION_START
    assert router.submit("   ").action != ACTION_STEER  # empty idle is ignored, not steer


def test_awaiting_reaction_submit_still_answers_not_steer():
    """The interrupt fork must not hijack the normal awaiting-reaction path."""
    router = InputRouter()
    router.begin_run()
    req = UiRequest(kind=REQUEST_REACTION, prompt="ok?")
    router.on_request(req)
    outcome = router.submit("looks good")
    assert outcome.action == ACTION_ANSWER
    assert req.answer == "looks good"


# --- Session preservation (the durable state esc must NOT reset) -------------


def test_session_holds_durable_state(tmp_path):
    session = Session(tmp_path)
    # The working dir, transcript, memory, queue, history all live on the session.
    assert session.working_dir == tmp_path.resolve()
    assert session.transcript is not None
    assert session.memory is not None
    assert isinstance(session.queue, InputQueue)
    assert session.goal is None and session.last_plan is None and session.revisions == 0


def test_session_reset_wipes_everything_but_is_separate_from_stop(tmp_path):
    session = Session(tmp_path)
    session.transcript.record("user", "goal", "build x")
    session.goal = "build x"
    session.revisions = 2
    session.queue.enqueue("later task")
    before_transcript = session.transcript

    session.reset()  # /clear
    assert session.transcript is not before_transcript  # a fresh thread
    assert session.transcript.turns == []
    assert session.goal is None
    assert session.revisions == 0
    assert len(session.queue) == 0


def test_steer_revision_accounting(tmp_path):
    session = Session(tmp_path)
    assert session.can_steer(max_revisions=2) is True
    assert session.note_steer() == 1
    assert session.can_steer(max_revisions=2) is True
    assert session.note_steer() == 2
    assert session.can_steer(max_revisions=2) is False  # budget reached
