"""The sync <-> async bridge: Relay's blocking engine talking to an async UI.

Relay's engine is synchronous and BLOCKS: ``call_model`` blocks, and the human
seams (``user_turn``, ``user_decision``, ``approver``) block the calling thread
waiting for an answer. A Textual UI runs an async event loop that must NEVER
block. This module is the seam between the two, built deliberately free of any
UI framework so it is testable headless:

- The engine runs on a WORKER thread; the UI stays on its async loop.
- **Engine -> UI:** events and "I need an answer" requests are handed to
  callbacks (``on_event`` / ``on_request``) that fire ON THE WORKER THREAD.
  The UI wraps them in its own thread-safe marshal (Textual's
  ``App.call_from_thread``); tests use plain list-appenders. The bridge never
  touches a widget.
- **UI -> engine:** each blocking ask parks the worker on a
  :class:`threading.Event` inside a :class:`UiRequest`; the UI (any thread)
  calls :meth:`UiRequest.deliver` with the user's typed answer, which unblocks
  the worker and becomes the callback's return value. Only the worker ever
  blocks.

Get this right and the engine never knows it is talking to a TUI instead of a
terminal -- the TUI is just another renderer.

Cancellation is the money-leak guard: :meth:`EngineBridge.cancel` both flags
``should_cancel`` (consulted by ``run_planned`` at step boundaries) and
unblocks any pending ask by raising :class:`BridgeCancelled` on the worker, so
a quitting UI can always join the thread instead of orphaning a worker that
keeps calling the API and billing the account.

:class:`InputRouter` is the UI-side input state machine: ONE text box serves
many purposes depending on what the engine is waiting for, and the routing is
explicit (a state enum + a deliver-to-the-waiting-callback path), not implicit
in widget callbacks. It is UI-thread-only by design (not thread-safe).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

from relay.config import ModelConfig
from relay.conversation import DEFAULT_MAX_ROUNDS, ConversationResult, plan_conversationally
from relay.orchestrator import (
    STATUS_CANCELLED,
    STATUS_DECLINED,
    Event,
    PlannedTaskResult,
    run_planned,
)
from relay.telemetry import Ledger
from relay.transcript import Transcript

# The kinds of blocking ask the engine can park on. Each maps 1:1 to an engine
# seam: reaction <- ``user_turn``, decision <- ``user_decision``,
# approval <- ``approver`` (CONFIRM-category bash).
REQUEST_REACTION = "reaction"
REQUEST_DECISION = "decision"
REQUEST_APPROVAL = "approval"

# Replies treated as "yes" for an approval ask (anything else denies -- the
# safe default for a gated command). Trailing punctuation is stripped first.
_APPROVAL_YES = {"y", "yes", "ok", "okay", "approve", "approved", "run", "run it", "go"}

# RunOutcome.status beyond the engine's own terminal statuses: the engine raised.
STATUS_ERROR = "error"

# Synthetic event kind marking the planning -> executing transition, so the UI
# (and its InputRouter) can track what "busy" currently means.
EVENT_PHASE = "phase"


class BridgeCancelled(Exception):
    """Raised ON THE WORKER THREAD when a blocking ask is cancelled.

    Propagates up through the engine seam (``user_turn`` / ``user_decision`` /
    ``approver``) and out of the engine call, so the worker unwinds promptly and
    can be joined -- the engine never proceeds on a guessed answer.
    """


@dataclass
class UiRequest:
    """One blocking question from the engine, awaiting a UI-delivered answer.

    The worker parks on :meth:`wait`; the UI settles the request exactly once,
    with :meth:`deliver` (an answer) or :meth:`cancel`. First settle wins --
    a late deliver after a cancel (or vice versa) is a no-op returning False.
    """

    kind: str  # one of REQUEST_REACTION / REQUEST_DECISION / REQUEST_APPROVAL
    prompt: str  # full-fidelity text of what the engine is asking
    answer: str | None = None
    cancelled: bool = False
    _settled: threading.Event = field(default_factory=threading.Event, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def deliver(self, answer: str) -> bool:
        """Deliver the user's answer, unblocking the waiting worker."""
        with self._lock:
            if self._settled.is_set():
                return False
            self.answer = answer
            self._settled.set()
            return True

    def cancel(self) -> bool:
        """Settle the request as cancelled (the waiting ask raises)."""
        with self._lock:
            if self._settled.is_set():
                return False
            self.cancelled = True
            self._settled.set()
            return True

    def wait(self, timeout: float | None = None) -> bool:
        """Block (the worker) until the request settles. True if it settled."""
        return self._settled.wait(timeout)

    @property
    def settled(self) -> bool:
        return self._settled.is_set()


class EngineBridge:
    """The engine side of the bridge: blocking asks + an ordered event feed.

    ``on_request`` / ``on_event`` are called ON THE WORKER THREAD; the UI is
    responsible for marshaling them onto its own thread (tests may consume them
    directly). :meth:`user_turn`, :meth:`user_decision` and :meth:`approver`
    plug straight into the engine's callback seams.
    """

    def __init__(
        self,
        *,
        on_request: Callable[[UiRequest], None],
        on_event: Callable[[Event], None],
    ) -> None:
        self._on_request = on_request
        self._on_event = on_event
        self._lock = threading.Lock()
        self._pending: UiRequest | None = None
        self._cancel = threading.Event()

    # -- engine -> UI (worker thread) ---------------------------------------

    def emit_event(self, event: Event) -> None:
        """Forward one engine event to the UI sink (in emission order)."""
        self._on_event(event)

    def ask(self, kind: str, prompt: str) -> str:
        """Block the WORKER on a question until the UI delivers an answer.

        Raises :class:`BridgeCancelled` if the bridge is (or becomes) cancelled
        while waiting, so a quitting UI can always unwind the worker.
        """
        if self._cancel.is_set():
            raise BridgeCancelled(kind)
        request = UiRequest(kind=kind, prompt=prompt)
        with self._lock:
            self._pending = request
            if self._cancel.is_set():  # cancel raced in: settle before parking
                request.cancel()
        self._on_request(request)
        request.wait()
        with self._lock:
            self._pending = None
        if request.cancelled:
            raise BridgeCancelled(kind)
        return request.answer or ""

    # -- the engine's callback seams (each just an ask of a specific kind) --

    def user_turn(self, prompt: str) -> str:
        """``plan_conversationally``'s seam: show a question/plan, get a reply."""
        return self.ask(REQUEST_REACTION, prompt)

    def user_decision(self, question: str) -> str:
        """``run_planned``'s escalation seam: a mid-run product decision."""
        return self.ask(REQUEST_DECISION, question)

    def approver(self, command: str, reason: str) -> bool:
        """``Tools``'s CONFIRM-bash seam: approve only on an explicit yes."""
        prompt = (
            f"The executor wants to run a gated command:\n  {command}\n"
            f"Why gated: {reason}\n"
            "Approve? (yes/no)"
        )
        answer = self.ask(REQUEST_APPROVAL, prompt)
        return _is_affirmative(answer)

    # -- UI -> engine control (any thread) -----------------------------------

    def cancel(self) -> None:
        """Request stop: flag ``should_cancel`` AND unblock any pending ask."""
        self._cancel.set()
        with self._lock:
            if self._pending is not None:
                self._pending.cancel()

    def should_cancel(self) -> bool:
        """The ``cancel_check`` for ``run_planned`` (step-boundary polling)."""
        return self._cancel.is_set()

    @property
    def pending(self) -> UiRequest | None:
        """The currently-parked request, if any (for the UI's state display)."""
        with self._lock:
            return self._pending


def _is_affirmative(answer: str) -> bool:
    return (answer or "").strip().lower().rstrip("!.") in _APPROVAL_YES


# --- the UI-side input state machine ----------------------------------------


class InputState(str, Enum):
    """What the one input box currently means (what the engine is waiting for)."""

    IDLE = "idle"                            # no run; submit starts a new goal
    PLANNING = "planning"                    # engine busy planning; submit ignored
    EXECUTING = "executing"                  # engine busy executing; submit ignored
    AWAITING_REACTION = "awaiting_reaction"  # user_turn parked; submit answers it
    AWAITING_DECISION = "awaiting_decision"  # user_decision parked; submit answers it
    AWAITING_APPROVAL = "awaiting_approval"  # approver parked; submit answers yes/no


_AWAITING_BY_KIND = {
    REQUEST_REACTION: InputState.AWAITING_REACTION,
    REQUEST_DECISION: InputState.AWAITING_DECISION,
    REQUEST_APPROVAL: InputState.AWAITING_APPROVAL,
}

# SubmitOutcome.action values.
ACTION_START = "start"      # idle submit: begin a new goal
ACTION_ANSWER = "answer"    # delivered to the parked request
ACTION_IGNORED = "ignored"  # engine busy (or empty goal): nothing routed


@dataclass
class SubmitOutcome:
    """Where one submitted line of text went."""

    action: str  # ACTION_START | ACTION_ANSWER | ACTION_IGNORED
    text: str = ""
    kind: str = ""  # the answered request's kind (when action == ACTION_ANSWER)


class InputRouter:
    """Routes the single input box by engine state. UI-thread-only by design.

    The UI drives it: :meth:`begin_run` when a goal starts, :meth:`on_request`
    when the bridge parks a request, :meth:`set_phase` on planning/executing
    transitions, :meth:`finish_run` when the run ends. :meth:`submit` is the one
    deliver-this-answer path -- no widget callback decides routing on its own.
    """

    def __init__(self) -> None:
        self.state = InputState.IDLE
        self._pending: UiRequest | None = None
        self._busy = InputState.PLANNING  # what "busy" currently means

    @property
    def pending(self) -> UiRequest | None:
        return self._pending

    def begin_run(self) -> None:
        self._pending = None
        self._busy = InputState.PLANNING
        self.state = InputState.PLANNING

    def set_phase(self, phase: str) -> None:
        """Track the engine's busy phase ("planning" -> "executing")."""
        self._busy = InputState.EXECUTING if phase == "executing" else InputState.PLANNING
        if self._pending is None and self.state is not InputState.IDLE:
            self.state = self._busy

    def on_request(self, request: UiRequest) -> None:
        """A bridge request arrived: the next submit answers it."""
        self._pending = request
        self.state = _AWAITING_BY_KIND.get(request.kind, InputState.AWAITING_REACTION)

    def finish_run(self) -> None:
        self._pending = None
        self.state = InputState.IDLE

    def submit(self, text: str) -> SubmitOutcome:
        """Route one submitted line: start a goal, answer the parked ask, or ignore."""
        if self.state is InputState.IDLE:
            if not text.strip():
                return SubmitOutcome(ACTION_IGNORED, text)
            return SubmitOutcome(ACTION_START, text)
        request = self._pending
        if request is not None:
            self._pending = None
            self.state = self._busy
            request.deliver(text)
            return SubmitOutcome(ACTION_ANSWER, text, kind=request.kind)
        return SubmitOutcome(ACTION_IGNORED, text)  # busy: not misrouted, just dropped


# --- the worker-thread runner -------------------------------------------------


@dataclass
class RunOutcome:
    """How a bridge-driven run ended: a terminal status plus the artifacts.

    ``status`` is the engine's own terminal status (``completed``,
    ``cancelled``, ``declined_by_user``, ...) or :data:`STATUS_ERROR` when the
    engine raised (``error`` carries the one-line reason). ``result`` is None
    when execution never started (declined / cancelled during planning / error).
    """

    status: str
    result: PlannedTaskResult | None = None
    conversation: ConversationResult | None = None
    error: str = ""


class EngineRunner:
    """One conversational arc (plan -> commit -> execute) on a worker thread.

    Owns the :class:`EngineBridge` and the ONE :class:`Transcript` shared by
    ``plan_conversationally`` and ``run_planned``, so a mid-run escalation is
    the next turn of the same dialogue. Single-use: one goal per runner.

    ``on_request`` / ``on_event`` / ``on_finished`` all fire ON THE WORKER
    THREAD -- the UI must marshal them (tests consume them directly).
    :meth:`cancel` + :meth:`join` are the clean-shutdown pair: cancel unblocks
    any pending ask and stops the loop at the next step boundary, so a quitting
    UI never leaves an orphaned worker still calling the API.
    """

    def __init__(
        self,
        project_root: str | Path,
        *,
        models: ModelConfig | None = None,
        ledger: Ledger | None = None,
        client: object | None = None,
        assumption_level: str = "auto",
        auto_approve: bool = False,
        max_rounds: int = DEFAULT_MAX_ROUNDS,
        on_request: Callable[[UiRequest], None],
        on_event: Callable[[Event], None],
        on_finished: Callable[[RunOutcome], None],
        run_kwargs: dict | None = None,
    ) -> None:
        self.project_root = Path(project_root)
        self.models = models
        self.ledger = ledger if ledger is not None else Ledger()
        self.client = client
        self.assumption_level = assumption_level
        self.auto_approve = auto_approve
        self.max_rounds = max_rounds
        self.bridge = EngineBridge(on_request=on_request, on_event=on_event)
        self.transcript = Transcript()  # the one thread across planning + execution
        self.outcome: RunOutcome | None = None
        self._on_finished = on_finished
        self._run_kwargs = dict(run_kwargs or {})  # extra run_planned knobs (tests)
        self._thread: threading.Thread | None = None

    # -- lifecycle -----------------------------------------------------------

    def start(self, goal: str) -> None:
        """Spawn the worker and drive the arc; UI callbacks stream the run."""
        if self._thread is not None:
            raise RuntimeError("EngineRunner is single-use: one run per runner")
        self._thread = threading.Thread(
            target=self._run, args=(goal,), name="relay-engine", daemon=True
        )
        self._thread.start()

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def cancel(self) -> None:
        """Request stop: unblocks a pending ask now, stops at the next boundary."""
        self.bridge.cancel()

    def join(self, timeout: float | None = None) -> bool:
        """Bounded wait for the worker to end. True when it is no longer alive."""
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    # -- the worker ----------------------------------------------------------

    def _run(self, goal: str) -> None:
        bridge = self.bridge
        try:
            bridge.emit_event(Event(EVENT_PHASE, "planning", {"phase": "planning"}))
            conversation = plan_conversationally(
                goal, self.project_root, models=self.models, ledger=self.ledger,
                client=self.client, assumption_level=self.assumption_level,
                user_turn=bridge.user_turn, max_rounds=self.max_rounds,
                on_event=lambda kind, message, payload: bridge.emit_event(
                    Event(kind, message, payload)
                ),
                transcript=self.transcript,
            )
            if not conversation.committed or conversation.plan is None or not conversation.plan.steps:
                outcome = RunOutcome(status=STATUS_DECLINED, conversation=conversation)
            else:
                bridge.emit_event(Event(EVENT_PHASE, "executing", {"phase": "executing"}))
                result = run_planned(
                    goal, self.project_root, models=self.models, ledger=self.ledger,
                    client=self.client, approver=bridge.approver,
                    auto_approve=self.auto_approve, user_decision=bridge.user_decision,
                    assumption_level=self.assumption_level,
                    committed_plan=conversation.plan, on_event=bridge.emit_event,
                    transcript=self.transcript, cancel_check=bridge.should_cancel,
                    **self._run_kwargs,
                )
                outcome = RunOutcome(status=result.status, result=result,
                                     conversation=conversation)
        except BridgeCancelled:
            # A pending ask was cancelled (quit/cancel while the engine waited);
            # the worker unwinds without guessing an answer.
            outcome = RunOutcome(status=STATUS_CANCELLED)
        except Exception as exc:  # noqa: BLE001 -- the UI must hear about ANY failure
            outcome = RunOutcome(status=STATUS_ERROR,
                                 error=f"{exc.__class__.__name__}: {exc}")
        self.outcome = outcome
        self._on_finished(outcome)
