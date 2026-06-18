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

from relay.config import ModelConfig, resolve_max_total_steps
from relay.conversation import DEFAULT_MAX_ROUNDS, ConversationResult, plan_conversationally
from relay.memory import PlanMemory
from relay.orchestrator import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_DECLINED,
    Event,
    PlannedTaskResult,
    run_planned,
)
from relay.planner import Plan, replan
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
    INTERRUPTED = "interrupted"              # esc halted the run; type=steer, empty/esc=stop


_AWAITING_BY_KIND = {
    REQUEST_REACTION: InputState.AWAITING_REACTION,
    REQUEST_DECISION: InputState.AWAITING_DECISION,
    REQUEST_APPROVAL: InputState.AWAITING_APPROVAL,
}

# SubmitOutcome.action values.
ACTION_START = "start"      # idle submit: begin a new goal
ACTION_ANSWER = "answer"    # delivered to the parked request
ACTION_IGNORED = "ignored"  # engine busy (or empty goal): nothing routed
ACTION_STEER = "steer"      # interrupted + typed input: redirect now (replan remainder)
ACTION_STOP = "stop"        # interrupted + empty/esc-again: abandon the plan, keep session


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

    def interrupt(self) -> None:
        """Enter the INTERRUPTED state: the run has halted at the clean boundary and
        the user is at the interrupt prompt. The SESSION is untouched -- this only
        changes what the one input box now means (type=steer, empty/esc=stop)."""
        self._pending = None
        self.state = InputState.INTERRUPTED

    @property
    def interrupted(self) -> bool:
        return self.state is InputState.INTERRUPTED

    def submit(self, text: str) -> SubmitOutcome:
        """Route one submitted line: start a goal, answer the parked ask, steer/stop
        an interrupt, or ignore."""
        if self.state is InputState.INTERRUPTED:
            # The fork: typed input redirects NOW (steer); empty means STOP (abandon
            # the plan, keep the session). An interrupt usually means "fix this now",
            # so a non-empty submit defaults to steer.
            if not text.strip():
                self.state = InputState.IDLE  # stop: back to a clean idle session
                return SubmitOutcome(ACTION_STOP, text)
            self.state = InputState.PLANNING  # the steer kicks off a continuation run
            return SubmitOutcome(ACTION_STEER, text)
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

    ``working_dir`` is the directory a COMPLETED run established as the new
    session working dir, if any (None otherwise). It is the extension point the
    session adopts from -- and the gate (see :class:`WorkingDirSession`) ensures a
    cwd change carried by a non-completed run is never silently adopted.
    """

    status: str
    result: PlannedTaskResult | None = None
    conversation: ConversationResult | None = None
    error: str = ""
    working_dir: str | None = None


class WorkingDirSession:
    """The session-sticky working directory: the base for the NEXT run's root.

    Relay used to re-default every goal's root to the immutable LAUNCH root, so a
    working dir the user had established (e.g. "we're working in lunar_lander_testing")
    evaporated the moment they cancelled a plan and started a new goal -- the new
    goal built in the launch root instead. This holds the effective working dir as
    SESSION state: once established it persists across subsequent goals until
    explicitly changed, rather than resetting each run.

    Two ways a working dir becomes established, and one guard:
      - :meth:`set_working_dir` -- the user (or brain) sets it explicitly. Immediate
        and persistent.
      - :meth:`adopt_from_outcome` -- a COMPLETED run that reported a new working dir
        has it adopted. A cancelled / declined / errored run reports nothing adoptable,
        so a cwd change that lived only in a never-executed (cancelled) plan can NEVER
        silently take effect.

    Paths resolve relative to the CURRENT working dir (so a relative change is
    relative to where we are now), and are confined to the launch root's tree is
    NOT enforced here -- the engine's :class:`~relay.tools.Tools` sandbox confines
    file/bash ops within whatever root it is handed; this only decides that root.
    """

    def __init__(self, launch_root: str | Path) -> None:
        self.launch_root = Path(launch_root).resolve()
        self._cwd = self.launch_root

    @property
    def working_dir(self) -> Path:
        """The effective working dir -- the base/root for the next run."""
        return self._cwd

    def is_launch_root(self) -> bool:
        """Whether the working dir is still the original launch root (unchanged)."""
        return self._cwd == self.launch_root

    def set_working_dir(self, path: str | Path) -> Path:
        """Explicitly establish a new working dir (resolved; immediate; persistent).

        A relative ``path`` is resolved against the CURRENT working dir, so
        successive relative moves compose. Returns the resolved directory.
        """
        self._cwd = self._resolve(path)
        return self._cwd

    def adopt_from_outcome(self, outcome: RunOutcome | None) -> bool:
        """Adopt a run's established working dir ONLY if the run completed.

        Returns True when the working dir changed. A non-completed outcome (cancelled,
        declined, error) is left untouched -- the structural guard that a cancelled-plan
        cwd change does not silently persist.
        """
        if outcome is None or outcome.status != STATUS_COMPLETED:
            return False
        target = outcome.working_dir
        if not target:
            return False
        resolved = self._resolve(target)
        changed = resolved != self._cwd
        self._cwd = resolved
        return changed

    def _resolve(self, path: str | Path) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self._cwd / candidate
        return candidate.resolve()


class Session(WorkingDirSession):
    """The durable SESSION: everything that outlives an individual run.

    Relay's interrupt model (v0.0.28) makes esc halt the *run*, not the session.
    Steering (replan-remainder) and queue consumption start NEW runs that must
    continue the SAME conversation, memory, working dir, and cost -- so those live
    here, on the session, and are threaded into each :class:`EngineRunner` rather
    than recreated per run. Only ``/clear`` (:meth:`reset`) wipes the session.

    Holds (beyond the working dir from :class:`WorkingDirSession`):
      - ``transcript`` / ``memory``: the one continuous thread + learnings.
      - ``goal``: the overall goal (so a steer can replan the remainder).
      - ``last_plan``: the plan-so-far from the most recent run (a steer replans its
        remainder; completed steps stay done).
      - ``revisions``: how many steers have been applied (a steer counts as a plan
        revision -- bounded by ``max_plan_revisions``).
      - ``queue`` / ``history``: the input queue + recall history (Part 3).
    """

    def __init__(self, launch_root: str | Path) -> None:
        super().__init__(launch_root)
        self.transcript = Transcript()
        self.memory = PlanMemory()
        self.goal: str | None = None
        self.last_plan: Plan | None = None
        self.revisions = 0
        self.queue = InputQueue()
        self.history = InputHistory()

    def reset(self) -> None:
        """Full-session reset (``/clear``): wipe conversation, memory, plan, queue,
        and recall history, and start fresh. Distinct from STOP, which abandons the
        plan but keeps all of this. The working DIR is left as-is (it is a workspace
        location, not conversation/memory/plan)."""
        self.transcript = Transcript()
        self.memory = PlanMemory()
        self.goal = None
        self.last_plan = None
        self.revisions = 0
        self.queue = InputQueue()
        self.history = InputHistory()

    def note_steer(self) -> int:
        """Record that a steer (a plan revision) is being applied; return the new count."""
        self.revisions += 1
        return self.revisions

    def can_steer(self, max_revisions: int) -> bool:
        """Whether another steer is within the plan-revision budget."""
        return self.revisions < max_revisions


class InputQueue:
    """An ordered FIFO of inputs to pick up when the current step/plan finishes.

    ``/queue <input>`` appends; on a run's clean completion the next item is
    consumed (dequeued) and started as the next direction within the SAME session.
    Deterministic and ordered -- no priority, no dedup. UI-thread-only by design.
    """

    def __init__(self) -> None:
        self._items: list[str] = []

    def enqueue(self, text: str) -> None:
        self._items.append(text)

    def dequeue(self) -> str | None:
        return self._items.pop(0) if self._items else None

    def pending(self) -> list[str]:
        return list(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __bool__(self) -> bool:
        return bool(self._items)


class InputHistory:
    """Up-arrow recall: ONE unified "recall and edit" history of the user's inputs.

    Records goals, steers, and queued items in order. :meth:`recall_older` walks
    back toward the oldest entry; :meth:`recall_newer` walks forward. The cursor
    resets on :meth:`add` (a new submission), so the next up-arrow starts from the
    most recent. There is no second/competing up-arrow behavior -- this is it.
    """

    def __init__(self) -> None:
        self._items: list[str] = []
        self._cursor: int | None = None  # None = not browsing; else index into _items

    def add(self, text: str) -> None:
        if text.strip():
            self._items.append(text)
        self._cursor = None  # a fresh submission resets recall to the newest

    def recall_older(self) -> str | None:
        """The previous (older) input, moving back one each call. None if empty."""
        if not self._items:
            return None
        if self._cursor is None:
            self._cursor = len(self._items) - 1
        elif self._cursor > 0:
            self._cursor -= 1
        return self._items[self._cursor]

    def recall_newer(self) -> str | None:
        """The next (newer) input, moving forward; '' once past the newest."""
        if not self._items or self._cursor is None:
            return None
        if self._cursor < len(self._items) - 1:
            self._cursor += 1
            return self._items[self._cursor]
        self._cursor = None
        return ""  # stepped past the newest: a cleared field

    def items(self) -> list[str]:
        return list(self._items)


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
        transcript: Transcript | None = None,
        memory: PlanMemory | None = None,
    ) -> None:
        self.project_root = Path(project_root)
        self.models = models
        self.ledger = ledger if ledger is not None else Ledger()
        self.client = client
        self.assumption_level = assumption_level
        self.auto_approve = auto_approve
        self.max_rounds = max_rounds
        self.bridge = EngineBridge(on_request=on_request, on_event=on_event)
        # The transcript + memory are SESSION-owned (passed in) so a steer/queue
        # continuation run keeps the same conversation + learnings; a bare runner
        # (tests) gets fresh ones, unchanged from before.
        self.transcript = transcript if transcript is not None else Transcript()
        self.memory = memory if memory is not None else PlanMemory()
        self.outcome: RunOutcome | None = None
        self._on_finished = on_finished
        self._run_kwargs = dict(run_kwargs or {})  # extra run_planned knobs (tests)
        # The TUI is a production caller: default the global step ceiling (50, via
        # env/config) so a stuck run can't grind unbounded. An explicit run_kwargs
        # value (tests) is respected; setdefault only fills it when absent.
        self._run_kwargs.setdefault("max_total_steps", resolve_max_total_steps())
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

    def start_steer(self, goal: str, prior_plan: Plan, steer: str) -> None:
        """Spawn a STEER continuation: replan the REMAINDER of ``prior_plan`` with the
        user's ``steer`` input folded in as new direction, then execute the revision.

        Reuses the existing ``replan`` machinery (text-protocol, no native
        tool-calling); the brain produces a revised plan for what's left and
        completed steps are not redone. The session's transcript + memory are reused
        so the continuation is the same conversation."""
        if self._thread is not None:
            raise RuntimeError("EngineRunner is single-use: one run per runner")
        self._thread = threading.Thread(
            target=self._run_steer, args=(goal, prior_plan, steer),
            name="relay-engine-steer", daemon=True,
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
                    assumption_level=self.assumption_level, memory=self.memory,
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

    def _run_steer(self, goal: str, prior_plan: Plan, steer: str) -> None:
        """Worker for a steer: replan ``prior_plan``'s remainder with ``steer`` folded
        in, then execute the revision (resuming the same session thread)."""
        bridge = self.bridge
        try:
            bridge.emit_event(Event(EVENT_PHASE, "planning", {"phase": "planning"}))
            # The step that was in-flight when the user interrupted (or the last step).
            interrupted = prior_plan.next_pending() or (
                prior_plan.steps[-1] if prior_plan.steps else None
            )
            revised = replan(
                goal, prior_plan, interrupted, "user interrupted and redirected",
                prior_plan.completed_outcomes(), models=self.models, ledger=self.ledger,
                client=self.client, memory=self.memory, steer=steer,
            )
            if revised is None or not revised.steps:
                # The brain aborted, or produced nothing executable: end cleanly; the
                # session is preserved and the user can try again.
                outcome = RunOutcome(status=STATUS_DECLINED)
            else:
                bridge.emit_event(Event(EVENT_PHASE, "executing", {"phase": "executing"}))
                result = run_planned(
                    goal, self.project_root, models=self.models, ledger=self.ledger,
                    client=self.client, approver=bridge.approver,
                    auto_approve=self.auto_approve, user_decision=bridge.user_decision,
                    assumption_level=self.assumption_level, memory=self.memory,
                    committed_plan=revised, on_event=bridge.emit_event,
                    transcript=self.transcript, cancel_check=bridge.should_cancel,
                    **self._run_kwargs,
                )
                outcome = RunOutcome(status=result.status, result=result)
        except BridgeCancelled:
            outcome = RunOutcome(status=STATUS_CANCELLED)
        except Exception as exc:  # noqa: BLE001 -- the UI must hear about ANY failure
            outcome = RunOutcome(status=STATUS_ERROR,
                                 error=f"{exc.__class__.__name__}: {exc}")
        self.outcome = outcome
        self._on_finished(outcome)
