"""The Relay TUI: a two-pane chat over the engine, via the v0.0.11 bridge.

The TUI is JUST ANOTHER RENDERER. The engine already emits events
(``on_event``) and asks through blocking callbacks (``user_turn``,
``user_decision``, ``approver``); this app renders the events and answers the
callbacks. No planning or execution logic lives here -- if the UI ever needs to
reimplement engine behavior, the split is breaking.

Threading: the engine runs on :class:`~relay.bridge.EngineRunner`'s worker
thread; this app stays on Textual's async loop. Every bridge callback fires on
the worker and is marshaled here with ``App.call_from_thread`` -- nothing
touches a widget cross-thread. Answers travel back through
:class:`~relay.bridge.InputRouter`'s single deliver-this-answer path.

Layout (minimal, v1): a Conversation pane (the transcript thread -- the star),
an Activity pane (the noisy event firehose, kept OUT of the conversation), a
one-line status/model indicator, and the input box. The conversation render
path is UNICODE-CLEAN: turn text is never ASCII-sanitized here (the recurring
cp1252 hazard belongs to the legacy console, not Textual).

:func:`present_prompt` is the ONE chokepoint every user-facing question/prompt
string passes through before display. Today it is a pass-through; prompt 2's
experience-level projection slots in there without a refactor.
"""

from __future__ import annotations

import asyncio

from textual.app import App, ComposeResult
from textual.widgets import Input, RichLog, Static

from relay.bridge import (
    ACTION_ANSWER,
    ACTION_START,
    EVENT_PHASE,
    REQUEST_APPROVAL,
    STATUS_ERROR,
    EngineRunner,
    InputRouter,
    InputState,
    RunOutcome,
    UiRequest,
)
from relay.config import ModelConfig, load_models
from relay.orchestrator import Event
from relay.transcript import Turn

# How often the conversation pane catches up with the (append-only) transcript.
_SYNC_INTERVAL_S = 0.2
# Bounded wait when joining the worker on quit -- never hang the exit.
_JOIN_TIMEOUT_S = 5.0

# Human labels for the status line, per input state.
_STATE_HINTS = {
    InputState.IDLE: "type a goal to start",
    InputState.PLANNING: "planning... (esc to cancel)",
    InputState.EXECUTING: "executing... (esc to cancel)",
    InputState.AWAITING_REACTION: "react to the plan ('ok' commits)",
    InputState.AWAITING_DECISION: "the agent needs your decision",
    InputState.AWAITING_APPROVAL: "approve the command? (yes/no)",
}


def present_prompt(text: str) -> str:
    """THE chokepoint for every user-facing question/prompt string.

    v1 passes full-fidelity text through unchanged. Prompt 2's
    experience-level projection (rephrasing per user expertise) plugs in here
    -- one place, no refactor.
    """
    return text


def format_turn(turn: Turn) -> str:
    """Render one transcript turn for the conversation pane.

    UNICODE-CLEAN by contract: the turn text is passed through verbatim (no
    ASCII sanitizing, no ellipsis truncation) -- Textual renders real unicode
    natively, unlike the legacy Windows console path.
    """
    who = "you" if turn.speaker == "user" else "brain"
    return f"{who} ({turn.phase}): {turn.text}"


class RelayTuiApp(App):
    """Two panes + one input box over a worker-thread engine run."""

    TITLE = "Relay"

    CSS = """
    Screen { layout: vertical; }
    #conversation {
        height: 2fr;
        border: round $primary;
        padding: 0 1;
    }
    #activity {
        height: 1fr;
        border: round $secondary;
        padding: 0 1;
        color: $text-muted;
    }
    #status { height: 1; padding: 0 1; background: $surface; }
    """

    BINDINGS = [
        ("escape", "cancel_run", "Cancel run"),
        ("ctrl+q", "quit", "Quit"),
    ]

    def __init__(
        self,
        *,
        root: str = ".",
        models: ModelConfig | None = None,
        client: object | None = None,
        assumption_level: str = "auto",
        auto_approve: bool = False,
        run_kwargs: dict | None = None,
    ) -> None:
        super().__init__()
        self._root = root
        self._models = models if models is not None else load_models()
        self._client = client
        self._assumption_level = assumption_level
        self._auto_approve = auto_approve
        self._run_kwargs = run_kwargs
        self._router = InputRouter()
        self._runner: EngineRunner | None = None
        self._quitting = False
        # The render-path buffers: exactly the strings handed to the widgets,
        # kept so headless tests can assert on the render path directly.
        self._conversation_lines: list[str] = []
        self._activity_lines: list[str] = []
        self._status_text = ""
        self._seen_turn_ids: set[str] = set()

    # -- layout ---------------------------------------------------------------

    def compose(self) -> ComposeResult:
        conversation = RichLog(id="conversation", wrap=True, markup=False, highlight=False)
        conversation.border_title = "Conversation"
        activity = RichLog(id="activity", wrap=True, markup=False, highlight=False)
        activity.border_title = "Activity"
        yield conversation
        yield activity
        yield Static(id="status")
        yield Input(id="prompt", placeholder="Type a goal and press Enter...")

    def on_mount(self) -> None:
        # The model indicator is visible from launch, BEFORE the first message.
        self._update_status()
        self.query_one("#prompt", Input).focus()
        self.set_interval(_SYNC_INTERVAL_S, self._sync_transcript)

    # -- the input box (one box, routed by engine state) -----------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value
        event.input.value = ""
        outcome = self._router.submit(text)
        if outcome.action == ACTION_START:
            self._start_run(text)
        elif outcome.action == ACTION_ANSWER:
            # Answers that become transcript turns render via the sync pass;
            # approval answers never reach the transcript, so echo them here.
            if outcome.kind == REQUEST_APPROVAL:
                self._write_conversation(f"you (approval): {text}")
        elif text.strip():
            self._write_activity("(input ignored: the engine is busy)")
        self._update_status()

    def _start_run(self, goal: str) -> None:
        self._seen_turn_ids.clear()  # fresh transcript: turn ids restart at t0
        if self._conversation_lines:
            self._write_conversation("")  # a blank line between runs
        self._write_conversation(f"you (goal): {goal}")
        self._router.begin_run()
        self._runner = EngineRunner(
            self._root,
            models=self._models,
            client=self._client,
            assumption_level=self._assumption_level,
            auto_approve=self._auto_approve,
            on_request=self._marshal(self._handle_request),
            on_event=self._marshal(self._handle_event),
            on_finished=self._marshal(self._handle_finished),
            run_kwargs=self._run_kwargs,
        )
        self._runner.start(goal)

    # -- worker -> UI marshaling (the only crossing) ----------------------------

    def _marshal(self, handler):
        """Wrap a UI handler so bridge callbacks (worker thread) reach it safely."""

        def callback(*args) -> None:
            if self._quitting:
                return  # shutting down: drop UI updates, let the worker unwind
            try:
                self.call_from_thread(handler, *args)
            except Exception:  # noqa: BLE001 -- app torn down mid-callback; drop it
                pass

        return callback

    def _handle_request(self, request: UiRequest) -> None:
        """A blocking ask arrived: show it, point the input box at it."""
        self._router.on_request(request)
        self._sync_transcript()
        # The transcript already carries most asks as turns (questions,
        # escalations); render the prompt only when it ADDS detail -- e.g. the
        # proposal's full plain plan behind its one-line headline turn.
        last_turn_text = self._last_synced_turn_text()
        if request.prompt.strip() != (last_turn_text or "").strip():
            for line in present_prompt(request.prompt).splitlines():
                self._write_conversation(f"brain: {line}" if line.strip() else "")
        self._update_status()

    def _handle_event(self, event: Event) -> None:
        """One engine event: phase changes steer the router; all land in Activity."""
        if event.kind == EVENT_PHASE:
            self._router.set_phase(event.payload.get("phase", ""))
        self._write_activity(f"[{event.kind}] {event.message}")
        self._sync_transcript()
        self._update_status()

    def _handle_finished(self, outcome: RunOutcome) -> None:
        self._sync_transcript()  # the result turn is in the transcript by now
        if outcome.status == STATUS_ERROR:
            self._write_conversation(f"brain (error): the run failed -- {outcome.error}")
        elif outcome.result is None:
            # No execution happened (declined, or cancelled mid-conversation),
            # so no result turn exists; close the thread visibly anyway.
            self._write_conversation(f"(run ended: {outcome.status}; nothing was executed)")
        cost = self._runner.ledger.total_cost() if self._runner is not None else None
        cost_note = "" if cost is None else f" (cost ${cost:.4f})"
        self._write_activity(f"[finished] {outcome.status}{cost_note}")
        self._router.finish_run()
        self._update_status()

    # -- conversation pane: rendered from the Transcript ------------------------

    def _sync_transcript(self) -> None:
        """Append transcript turns not yet rendered (id-deduplicated, in order).

        The transcript is append-only and its turns are frozen, so snapshotting
        the list from the UI thread while the worker appends is safe; ids (not
        indices) dedupe so compaction or re-sync can never double-render.
        """
        runner = self._runner
        if runner is None:
            return
        for turn in list(runner.transcript.turns):
            if turn.id in self._seen_turn_ids:
                continue
            self._seen_turn_ids.add(turn.id)
            text = format_turn(turn)
            if turn.speaker != "user":
                text = present_prompt(text)
            self._write_conversation(text)

    def _last_synced_turn_text(self) -> str | None:
        runner = self._runner
        if runner is None or not runner.transcript.turns:
            return None
        return runner.transcript.turns[-1].text

    # -- widget writes (the render path; buffers mirror the widgets for tests) --

    def _write_conversation(self, line: str) -> None:
        self._conversation_lines.append(line)
        self.query_one("#conversation", RichLog).write(line)

    def _write_activity(self, line: str) -> None:
        self._activity_lines.append(line)
        self.query_one("#activity", RichLog).write(line)

    def _update_status(self) -> None:
        state = self._router.state
        hint = _STATE_HINTS.get(state, "")
        self._status_text = (
            f"[{state.value}] {hint}  |  "
            f"brain={self._models.brain}  hands={self._models.hands}"
        )
        self.query_one("#status", Static).update(self._status_text)

    # -- cancel + clean shutdown (the money-leak guard) --------------------------

    def action_cancel_run(self) -> None:
        runner = self._runner
        if runner is not None and runner.is_running:
            runner.cancel()
            self._write_activity("[cancel] stop requested (takes effect at the next step boundary)")

    async def action_quit(self) -> None:
        """Quit WITHOUT orphaning the worker: cancel, join (bounded), then exit."""
        self._quitting = True
        runner = self._runner
        if runner is not None and runner.is_running:
            runner.cancel()
            # Join off the UI loop so in-flight call_from_thread marshals can
            # still drain (joining on-loop could deadlock until their timeout).
            await asyncio.get_running_loop().run_in_executor(
                None, runner.join, _JOIN_TIMEOUT_S
            )
        self.exit()
