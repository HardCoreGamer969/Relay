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
from types import SimpleNamespace

import pytest

from relay.bridge import (
    ACTION_ANSWER,
    ACTION_IGNORED,
    ACTION_START,
    REQUEST_APPROVAL,
    REQUEST_DECISION,
    REQUEST_REACTION,
    STATUS_ERROR,
    BridgeCancelled,
    EngineBridge,
    EngineRunner,
    InputRouter,
    InputState,
    UiRequest,
)
from relay.config import ModelConfig
from relay.loop import STATUS_COMPLETED
from relay.orchestrator import STATUS_CANCELLED, STATUS_DECLINED, Event

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


# --- the worker-thread runner: the full arc, headless -------------------------

CFG = ModelConfig(brain="vendor/brain", hands="vendor/hands")


def _resp(content):
    usage = SimpleNamespace(prompt_tokens=6, completion_tokens=4, total_tokens=10, cost=0.00002)
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))], usage=usage)


class _ArcClient:
    """A fake engine brain+hands: routes by model, then by brain system prompt."""

    def __init__(self, *, hands=(), plan="<plan><step>add login</step></plan>",
                 headline="A small login feature.", escalate=True):
        self._hands = list(hands)
        self._plan = plan
        self._headline = headline
        self._escalate = escalate
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
            return _resp(f"{self._plan}<headline>{self._headline}</headline>")
        if "asked a question mid-step" in system:
            if self._escalate:
                return _resp("<decision>escalate</decision>"
                             "<ask_user>Should login support OAuth?</ask_user>")
            return _resp("<decision>self_answer</decision><answer>use cookies</answer>")
        if "readable narrative" in system.lower():
            return _resp("Earlier, you asked for login and committed.")
        return _resp("<verdict>accept</verdict>")

    def hands_calls(self):
        return [c for c in self.calls if c["model"] == "vendor/hands"]


class _UiSink:
    """The headless stand-in for the UI: collects requests/events/outcomes."""

    def __init__(self):
        self.requests: list[UiRequest] = []
        self.events: list[Event] = []
        self.outcomes: list = []

    def runner(self, tmp_path, client, **kwargs):
        return EngineRunner(
            tmp_path, models=CFG, client=client,
            on_request=self.requests.append, on_event=self.events.append,
            on_finished=self.outcomes.append, **kwargs,
        )

    def next_request(self, n=1):
        assert _wait_for(lambda: len(self.requests) >= n), "no request arrived"
        return self.requests[n - 1]

    def finished(self):
        assert _wait_for(lambda: self.outcomes), "run never finished"
        return self.outcomes[0]


def test_runner_full_loop_one_thread_one_transcript(tmp_path):
    """Goal -> proposal -> commit -> escalation answered in the SAME thread -> done."""
    client = _ArcClient(hands=[
        "<question>do we need OAuth login?</question>",
        '<edit path="login.py">x</edit>\n<done>added login</done>',
    ])
    sink = _UiSink()
    runner = sink.runner(tmp_path, client)
    runner.start("add login")

    # 1. The proposal arrives as a blocking reaction ask (full plain plan).
    proposal = sink.next_request(1)
    assert proposal.kind == REQUEST_REACTION
    assert "add login" in proposal.prompt and "ok" in proposal.prompt
    proposal.deliver("ok")  # commit

    # 2. The mid-run escalation arrives as the NEXT ask, same box, same thread.
    escalation = sink.next_request(2)
    assert escalation.kind == REQUEST_DECISION
    assert "OAuth" in escalation.prompt
    escalation.deliver("yes, Google OAuth")

    # 3. The run completes; the worker is joinable; the work actually happened.
    outcome = sink.finished()
    assert outcome.status == STATUS_COMPLETED
    assert outcome.result is not None and outcome.result.done
    assert (tmp_path / "login.py").read_text(encoding="utf-8") == "x"
    assert runner.join(WAIT_S) and not runner.is_running

    # ONE continuous transcript: planning and the escalation are the same dialogue.
    assert outcome.result.transcript is runner.transcript
    phases = [t.phase for t in runner.transcript.turns]
    assert {"proposal", "commit", "escalation", "decision", "result"} <= set(phases)
    assert (phases.index("proposal") < phases.index("commit")
            < phases.index("escalation") < phases.index("decision")
            < phases.index("result"))

    # Events arrived in order: planning phase, then executing, then steps.
    kinds = [e.kind for e in sink.events]
    assert kinds.index("phase") < kinds.index("plan_proposed")
    executing = [i for i, e in enumerate(sink.events)
                 if e.kind == "phase" and e.payload.get("phase") == "executing"]
    assert executing and executing[0] < kinds.index("step_start")


def test_runner_decline_leaves_nothing_executed(tmp_path):
    client = _ArcClient()
    sink = _UiSink()
    runner = sink.runner(tmp_path, client, max_rounds=1)
    runner.start("add login")
    sink.next_request(1).deliver("no, forget it")  # not a commit phrase

    outcome = sink.finished()
    assert outcome.status == STATUS_DECLINED
    assert outcome.result is None
    assert client.hands_calls() == []  # nothing executed
    assert runner.join(WAIT_S)


def test_runner_cancel_while_parked_unblocks_and_joins(tmp_path):
    """The clean-shutdown contract: cancel + join, no orphaned worker."""
    client = _ArcClient()
    sink = _UiSink()
    runner = sink.runner(tmp_path, client)
    runner.start("add login")
    sink.next_request(1)  # parked on the proposal, mid-conversation
    assert runner.is_running

    runner.cancel()
    outcome = sink.finished()
    assert outcome.status == STATUS_CANCELLED
    assert runner.join(WAIT_S) and not runner.is_running
    assert client.hands_calls() == []  # no API spend after the cancel


def test_runner_cancel_stops_at_step_boundary(tmp_path):
    client = _ArcClient(
        plan="<plan><step>make a.txt</step><step>make b.txt</step></plan>",
        hands=[
            '<edit path="a.txt">A</edit>\n<done>made a.txt</done>',
            '<edit path="b.txt">B</edit>\n<done>made b.txt</done>',
        ],
    )
    sink = _UiSink()
    runner = sink.runner(tmp_path, client, run_kwargs={"supervise": False})

    base_on_event = sink.events.append
    def on_event(event):
        base_on_event(event)
        if event.kind == "step_done":
            runner.cancel()  # the user hits cancel as step 0 settles
    runner.bridge._on_event = on_event  # rewire before start (test-only seam)

    runner.start("two files")
    sink.next_request(1).deliver("ok")

    outcome = sink.finished()
    assert outcome.status == STATUS_CANCELLED
    assert outcome.result is not None and outcome.result.status == STATUS_CANCELLED
    assert len(client.hands_calls()) == 1  # step 1 never started
    assert (tmp_path / "a.txt").exists() and not (tmp_path / "b.txt").exists()
    assert runner.join(WAIT_S)


def test_runner_engine_failure_surfaces_as_error_outcome(tmp_path):
    class _Boom:
        def __init__(self):
            def explode(**kwargs):
                raise RuntimeError("boom")
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=explode))

    sink = _UiSink()
    runner = sink.runner(tmp_path, _Boom())
    runner.start("anything")
    outcome = sink.finished()
    assert outcome.status == STATUS_ERROR
    assert "boom" in outcome.error
    assert runner.join(WAIT_S)


def test_runner_is_single_use(tmp_path):
    sink = _UiSink()
    runner = sink.runner(tmp_path, _ArcClient())
    runner.start("goal one")
    with pytest.raises(RuntimeError):
        runner.start("goal two")
    runner.cancel()
    assert runner.join(WAIT_S)


# --- v0.0.21: the TUI is a production caller -> defaults the global ceiling ---


def _bare_runner(tmp_path, **kwargs):
    return EngineRunner(
        tmp_path, models=CFG, client=SimpleNamespace(),
        on_request=lambda r: None, on_event=lambda e: None, on_finished=lambda o: None,
        **kwargs,
    )


def test_bridge_defaults_global_step_ceiling(tmp_path, monkeypatch):
    monkeypatch.delenv("RELAY_MAX_TOTAL_STEPS", raising=False)
    monkeypatch.setenv("RELAY_CONFIG_DIR", str(tmp_path))  # empty config dir -> default 50
    runner = _bare_runner(tmp_path)
    assert runner._run_kwargs["max_total_steps"] == 50  # never unbounded in production


def test_bridge_explicit_ceiling_is_respected(tmp_path):
    # An explicit run_kwargs value (e.g. a test, or a future surface) wins over the
    # default -- setdefault only fills it when absent.
    runner = _bare_runner(tmp_path, run_kwargs={"max_total_steps": 3})
    assert runner._run_kwargs["max_total_steps"] == 3
