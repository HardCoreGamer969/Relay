"""Headless tests for the sync<->async TUI bridge (no Textual, no live terminal).

The bridge is deliberately UI-framework-free, so these tests stand in for the
UI: ``on_request`` / ``on_event`` callbacks append to plain lists, a real
worker thread plays the engine, and answers are delivered from the test (the
"UI") thread. This proves the round-trip contract -- the worker blocks, a
delivered answer unblocks it and becomes the callback's return value -- without
mounting an app or touching a terminal.
"""

from __future__ import annotations

import threading
import time

from relay.bridge import (
    ACTION_ANSWER,
    ACTION_IGNORED,
    ACTION_START,
    REQUEST_APPROVAL,
    REQUEST_DECISION,
    REQUEST_REACTION,
    BridgeCancelled,
    EngineBridge,
    InputRouter,
    InputState,
    UiRequest,
)
from relay.orchestrator import Event

WAIT_S = 5.0  # generous deadline; the asserts care about order, not speed


def _wait_for(predicate, timeout=WAIT_S):
    """Poll ``predicate`` until true or ``timeout``; returns the final value."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def _bridge(requests, events):
    return EngineBridge(on_request=requests.append, on_event=events.append)


def _start_worker(target):
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread


# --- blocking round-trips (the core contract) --------------------------------


def test_user_turn_round_trip_blocks_then_returns_delivered_answer():
    requests, events, answers = [], [], []
    bridge = _bridge(requests, events)

    thread = _start_worker(lambda: answers.append(bridge.user_turn("Which color?")))

    # The UI side receives the request while the worker stays blocked on it.
    assert _wait_for(lambda: requests)
    request = requests[0]
    assert request.kind == REQUEST_REACTION
    assert request.prompt == "Which color?"
    assert not request.settled
    assert thread.is_alive() and not answers  # parked, no answer yet

    # Delivering the answer unblocks the worker; the callback returns it.
    assert request.deliver("blue") is True
    thread.join(WAIT_S)
    assert not thread.is_alive()
    assert answers == ["blue"]


def test_user_decision_round_trip():
    requests, events, answers = [], [], []
    bridge = _bridge(requests, events)

    thread = _start_worker(lambda: answers.append(bridge.user_decision("OAuth or passwords?")))

    assert _wait_for(lambda: requests)
    assert requests[0].kind == REQUEST_DECISION
    assert thread.is_alive() and not answers
    requests[0].deliver("OAuth only")
    thread.join(WAIT_S)
    assert answers == ["OAuth only"]


def test_approver_parses_yes_and_no():
    requests, events, verdicts = [], [], []
    bridge = _bridge(requests, events)

    def worker():
        verdicts.append(bridge.approver("rm -rf build", "recursive delete"))
        verdicts.append(bridge.approver("git push --force", "force push"))

    thread = _start_worker(worker)

    assert _wait_for(lambda: len(requests) >= 1)
    assert requests[0].kind == REQUEST_APPROVAL
    assert "rm -rf build" in requests[0].prompt and "recursive delete" in requests[0].prompt
    requests[0].deliver("yes")
    assert _wait_for(lambda: len(requests) >= 2)
    requests[1].deliver("no")
    thread.join(WAIT_S)
    assert verdicts == [True, False]


def test_approver_denies_on_anything_unrecognized():
    requests, events, verdicts = [], [], []
    bridge = _bridge(requests, events)
    thread = _start_worker(lambda: verdicts.append(bridge.approver("pip install x", "installer")))
    assert _wait_for(lambda: requests)
    requests[0].deliver("hmm maybe?")  # not an explicit yes -> safe default: deny
    thread.join(WAIT_S)
    assert verdicts == [False]


# --- event marshaling ---------------------------------------------------------


def test_events_from_worker_reach_ui_sink_in_order():
    requests, events = [], []
    bridge = _bridge(requests, events)

    def worker():
        for i in range(200):
            bridge.emit_event(Event("tick", f"event {i}", {"i": i}))

    thread = _start_worker(worker)
    thread.join(WAIT_S)
    assert [e.payload["i"] for e in events] == list(range(200))


# --- cancellation (the money-leak guard) --------------------------------------


def test_cancel_unblocks_a_pending_ask_with_bridge_cancelled():
    requests, events, raised = [], [], []
    bridge = _bridge(requests, events)

    def worker():
        try:
            bridge.user_turn("still there?")
        except BridgeCancelled:
            raised.append(True)

    thread = _start_worker(worker)
    assert _wait_for(lambda: requests)
    assert thread.is_alive()

    bridge.cancel()
    thread.join(WAIT_S)
    assert not thread.is_alive()  # the worker is joinable, not orphaned
    assert raised == [True]
    assert bridge.should_cancel() is True


def test_ask_after_cancel_raises_immediately_without_a_request():
    requests, events, raised = [], [], []
    bridge = _bridge(requests, events)
    bridge.cancel()

    def worker():
        try:
            bridge.user_decision("too late?")
        except BridgeCancelled:
            raised.append(True)

    thread = _start_worker(worker)
    thread.join(WAIT_S)
    assert raised == [True]
    assert requests == []  # never even surfaced to the UI


def test_request_settles_exactly_once_first_settle_wins():
    request = UiRequest(kind=REQUEST_REACTION, prompt="?")
    assert request.deliver("first") is True
    assert request.cancel() is False  # late cancel: no-op
    assert request.deliver("second") is False  # late second answer: no-op
    assert request.answer == "first" and not request.cancelled

    cancelled = UiRequest(kind=REQUEST_REACTION, prompt="?")
    assert cancelled.cancel() is True
    assert cancelled.deliver("late") is False
    assert cancelled.cancelled and cancelled.answer is None


def test_delivered_answer_beats_a_racing_cancel():
    requests, events, answers = [], [], []
    bridge = _bridge(requests, events)
    thread = _start_worker(lambda: answers.append(bridge.user_turn("?")))
    assert _wait_for(lambda: requests)
    requests[0].deliver("kept")  # settles first...
    bridge.cancel()              # ...so the cancel cannot rewrite it
    thread.join(WAIT_S)
    assert answers == ["kept"]  # the real answer was returned, not dropped


# --- the input state machine ---------------------------------------------------


def test_idle_submit_starts_a_goal():
    router = InputRouter()
    outcome = router.submit("build a todo app")
    assert outcome.action == ACTION_START
    assert outcome.text == "build a todo app"
    assert router.state is InputState.IDLE  # the UI flips state via begin_run


def test_idle_empty_submit_is_ignored():
    router = InputRouter()
    assert router.submit("   ").action == ACTION_IGNORED


def test_submit_in_awaiting_reaction_answers_user_turn():
    router = InputRouter()
    router.begin_run()
    request = UiRequest(kind=REQUEST_REACTION, prompt="plan: ...")
    router.on_request(request)
    assert router.state is InputState.AWAITING_REACTION

    outcome = router.submit("ok")
    assert outcome.action == ACTION_ANSWER and outcome.kind == REQUEST_REACTION
    assert request.answer == "ok"  # delivered to the waiting callback
    assert router.state is InputState.PLANNING  # back to the busy phase


def test_submit_in_awaiting_decision_answers_user_decision():
    router = InputRouter()
    router.begin_run()
    router.set_phase("executing")
    request = UiRequest(kind=REQUEST_DECISION, prompt="OAuth?")
    router.on_request(request)
    assert router.state is InputState.AWAITING_DECISION

    outcome = router.submit("yes, Google OAuth")
    assert outcome.action == ACTION_ANSWER and outcome.kind == REQUEST_DECISION
    assert request.answer == "yes, Google OAuth"
    assert router.state is InputState.EXECUTING


def test_submit_in_awaiting_approval_routes_to_the_approval_request():
    router = InputRouter()
    router.begin_run()
    router.set_phase("executing")
    request = UiRequest(kind=REQUEST_APPROVAL, prompt="approve rm?")
    router.on_request(request)
    assert router.state is InputState.AWAITING_APPROVAL
    assert router.submit("no").kind == REQUEST_APPROVAL
    assert request.answer == "no"


def test_submit_while_busy_is_ignored_not_misrouted():
    router = InputRouter()
    router.begin_run()
    assert router.state is InputState.PLANNING
    assert router.submit("impatient typing").action == ACTION_IGNORED

    router.set_phase("executing")
    assert router.state is InputState.EXECUTING
    assert router.submit("more typing").action == ACTION_IGNORED


def test_phase_change_does_not_clobber_an_awaiting_state():
    router = InputRouter()
    router.begin_run()
    router.on_request(UiRequest(kind=REQUEST_DECISION, prompt="?"))
    router.set_phase("executing")  # phase event lands while a request is parked
    assert router.state is InputState.AWAITING_DECISION  # still awaiting


def test_finish_run_returns_to_idle():
    router = InputRouter()
    router.begin_run()
    router.set_phase("executing")
    router.finish_run()
    assert router.state is InputState.IDLE
    assert router.submit("next goal").action == ACTION_START
