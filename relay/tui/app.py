"""The Relay TUI: a welcome screen + a single live stream over the v0.0.11 bridge.

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

Two states:

- **Welcome** (no work yet): a composed, centered screen -- the letterspaced
  ``RELAY`` wordmark hero, a rotating greeting, the brain/hands pairing promoted
  as identity, and a dim hint. The stream is NOT shown here.
- **Working** (after the first goal): ONE live scrolling stream (v0.0.30,
  replacing the old two-pane Conversation/Activity split) interleaving -- in the
  order they happen -- the conversation (you/brain), the inline live plan (steps
  that update IN PLACE: done/active/pending with a spinner on the active one),
  tool calls, findings (a green hands->brain channel), and review verdicts; below
  it a status line (a breathing mode LED, step N/M, cost, cwd, queue) and the
  input box. brain = magenta, hands = cyan, findings = green. The first submit
  hands off from welcome to working (see :mod:`relay.tui` animations).

The conversation render path is UNICODE-CLEAN: turn text is never ASCII-
sanitized here (the recurring cp1252 hazard belongs to the legacy console, not
Textual). The welcome art uses unicode block glyphs freely.

:func:`present_prompt` is the ONE chokepoint every user-facing question/prompt
string passes through before display. Today it is a pass-through; prompt 2's
experience-level projection slots in there without a refactor.
"""

from __future__ import annotations

import asyncio
import logging
import platform
import random
from datetime import datetime
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.widgets import Input, Static
from textual.worker import Worker, WorkerState

_LOG = logging.getLogger("relay.tui")

from relay.bridge import (
    ACTION_ANSWER,
    ACTION_START,
    ACTION_STEER,
    ACTION_STOP,
    EVENT_PHASE,
    REQUEST_APPROVAL,
    REQUEST_REACTION,
    STATUS_ERROR,
    EngineRunner,
    InputRouter,
    InputState,
    RunOutcome,
    Session,
    UiRequest,
)
from relay.orchestrator import STATUS_CANCELLED
from relay.config import (
    ASSUMPTION_LEVELS,
    ROLES,
    ModelConfig,
    assumption_summary,
    describe_resolution,
    env_override_for,
    load_models,
    resolve_max_total_steps,
)
from relay.debug import build_debug_bundle, summarize_run
from relay.orchestrator import Event
from relay.providers import (
    DISCOVERY_LIST,
    known_providers,
    list_models as provider_list_models,
    resolve_provider,
    validate_model as provider_validate_model,
)
from relay.secrets import resolve_key

from .commands import (
    COMMANDS,
    Command,
    _parse_inline_command,
    _run_active,
    command_by_name,
    filter_commands,
    visible_commands,
)
from .dialogs import ApproveDialog, SegmentedControl, SelectDialog, TextEntryDialog
from .events import (
    describe_event_for_activity,
    format_turn,
    friendly_provider_error,
    model_identity,
    present_prompt,
)
from .input import PromptInput
from .plan import PLAN_MODES, render_plan_dock, resolve_plan_mode
from .setup import SetupScreen, _call_persist_role, _call_secrets_set_key
from .status import (
    StatusSnapshot,
    active_instruction,
    context_segment,
    cost_segment,
    mode_word,
    resolve_anim_mode,
    route_segment,
    step_segment,
)
from .stream import (
    STREAM_BUFFER_MAX,
    STREAM_MAX_LINES,
    find_in_lines,
    render_conversation_body,
    render_observation,
    stream_should_follow,
    tool_summary_line,
    trim_deque_list,
    trim_stream_children,
)
from .theme import (
    ACTOR_BRAIN,
    ACTOR_HANDS,
    C_AMBER,
    C_CYAN,
    C_DIM,
    C_GREEN,
    C_MAGENTA,
    C_MUTED,
    C_RED,
    C_TXT,
    RELAY_WORDMARK,
    W_BG,
    W_BG_CARD,
    W_BG_RAISED,
    W_BORDER,
    W_RED,
    W_TEXT,
    W_TEXT_DIM,
    W_TEXT_MUTED,
    W_WARN,
    _ACTOR_STYLES,
    _ANIM_FPS,
    _COST_PULSE_S,
    _GENERATING_STATES,
    _glitch_thresholds,
    _LED_INTERVAL_S,
    _normalize_block,
    _PLAN_ICON,
    _SPIN_INTERVAL_S,
    _SPINNER_FRAMES,
    _STARTUP_SHORT_S,
    _TRANSITION_SHORT_S,
    glitch_frame,
    pick_greeting,
    pick_placeholder,
    placeholder_for_state,
)

# Backup transcript sync (events are the primary path; this catches rare gaps).
_SYNC_INTERVAL_S = 1.0
# Bounded wait when joining the worker on quit -- never hang the exit.
_JOIN_TIMEOUT_S = 5.0

# U0/U1 keep stream/status/controller methods on RelayTuiApp; extraction is deferred
# to U2 to avoid risky mixin surgery during the package split.

class RelayTuiApp(App):
    """A welcome screen that hands off to a single live stream chat over the engine."""

    TITLE = "Relay"

    CSS = """
    Screen { layout: vertical; background: #050505; }

    /* -- the welcome state (shown first; hidden once work begins) -- */
    #welcome { height: 1fr; align: center middle; }
    #welcome-inner {
        width: auto;
        height: auto;
        align: center middle;
        padding: 1 4;
        border: double #ff0000;
        background: #0a0a0a;
    }
    #brand { width: auto; content-align: center middle; text-style: bold; color: #ff0000; }
    #greeting { width: auto; content-align: center middle; text-style: bold; margin-top: 1; color: #f0f0f0; }
    #indicator { width: auto; content-align: center middle; color: #888888; margin-top: 1; }
    #hint { width: auto; content-align: center middle; color: #555555; text-style: dim; margin-top: 1; }

    /* -- cockpit: status rail + plan dock + stream (U2) -- */
    #working { height: 1fr; layout: vertical; display: none; }
    #status {
        height: auto;
        min-height: 1;
        max-height: 2;
        padding: 0 1;
        background: #0a0a0a;
        border-bottom: solid #1a1a1a;
    }
    #cockpit-body { height: 1fr; layout: horizontal; }
    #plan-dock {
        width: 36;
        min-width: 24;
        max-width: 48;
        padding: 0 1;
        background: #0f0f0f;
        border-right: solid #1a1a1a;
    }
    #plan-dock.-hidden { display: none; width: 0; min-width: 0; max-width: 0; padding: 0; border: none; }
    #stream {
        height: 1fr;
        width: 1fr;
        padding: 0 1;
        background: #050505;
    }
    #stream .stream-row { width: 1fr; }

    /* -- the slash-command popover (shown only while typing a /command) -- */
    #command-popover {
        display: none;
        height: auto;
        max-height: 12;
        margin: 0 1;
        padding: 0 1;
        border: round #ff0000;
        background: #0f0f0f;
    }
    """

    BINDINGS = [
        ("escape", "cancel_run", "Cancel run"),
        ("ctrl+s", "open_setup", "Setup"),
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
        anim_mode: str = "short",
        list_models_fn=None,
        validate_fn=None,
        doctor_fn=None,
        runs_fn=None,
        catalog: object | None = None,
    ) -> None:
        super().__init__()
        self._root = root
        # The durable SESSION: the sticky working dir PLUS the continuous transcript,
        # memory, input queue, and recall history. esc halts a run but never the
        # session; only /clear resets it. Every run's root/transcript/memory are
        # threaded from here (see _start_run), so steer/queue continuations keep the
        # same conversation, learnings, cwd, and cost.
        self._session = Session(root)
        # esc set this while a run is in flight, so _handle_finished knows the clean
        # cancel was a user INTERRUPT (-> the interrupt prompt) vs. some other stop.
        self._interrupting = False
        # A steer requested via /redirect while a run was still executing: halt now,
        # then steer with this text the moment the run lands at the clean boundary.
        self._pending_steer: str | None = None
        self._models = models if models is not None else load_models()
        self._client = client
        # The model catalog is passed to run_planned so resolve_context_window can
        # read each actor's real context window from it (without it, the window
        # always falls to the 8192 default and memory budgets are stunted).
        self._catalog = catalog
        # Setup-flow seams (injected by tests; default to the real provider funcs).
        self._list_models_fn = list_models_fn
        self._validate_fn = validate_fn
        # Slash-command seams (injected by tests; default to the real CLI logic).
        self._doctor_fn = doctor_fn
        self._runs_fn = runs_fn
        self._pending_model_pick: dict | None = None
        # The slash-command popover state (mirrored for headless tests).
        self._popover_open = False
        self._popover_commands: list[Command] = []
        self._popover_index = 0
        self._assumption_level = assumption_level
        self._auto_approve = auto_approve
        # Always a dict: CLI launches with run_kwargs=None, and _start_steer/.get
        # must not AttributeError on interrupt→redirect / `/redirect`.
        self._run_kwargs = dict(run_kwargs or {})
        # TODO(prompt-2): drive anim_mode from persisted settings + a launch
        # counter (a longer "first few launches" variant for "long"). Hardcoded
        # "short" for now; "off" is a clean instant no-op.
        # Animation: non-default constructor arg wins; else RELAY_TUI_ANIM; else short.
        import os as _os
        if anim_mode != "short":
            self._anim_mode = anim_mode
        elif _os.environ.get("RELAY_TUI_ANIM") is not None:
            self._anim_mode = resolve_anim_mode(None)
        else:
            self._anim_mode = "short"
        self._anim_timer = None
        self._router = InputRouter()
        self._runner: EngineRunner | None = None
        self._quitting = False
        # "welcome" until the first goal hands off to "working" (one-way).
        self._view = "welcome"
        self._greeting = pick_greeting()
        self._placeholder = pick_placeholder()  # the idle prompt phrase for this launch
        self._indicator_text = model_identity(self._models)
        # The last "your save was shadowed by an env var" note (mirrored for tests;
        # "" when the most recent save landed as the resolved value).
        self._save_notice = ""
        # Two-tier cost (v0.0.20), both mirrored for tests; Relay SHOWS spend and lets
        # the user stop -- it never caps:
        #  - _goal_cost: the CURRENT goal's live cost; reset on a new goal but kept
        #    showing the last goal's total while idle (never blinks to $0 on finish).
        #  - _session_cost: cumulative over FINISHED goals this session; folded at
        #    finish, cleared only on quit or a manual /cost reset.
        self._goal_cost = 0.0
        self._session_cost = 0.0
        self._cost_visible = True  # status-line counter shown by default (/cost toggles)
        self._cost_pulse = False   # transient highlight while the counter is climbing
        self._cost_pulse_timer = None
        self._stopping = False     # esc pressed; awaiting the next safe stop boundary
        self._first_run = False  # set when the empty-state setup is offered on launch
        # The render-path buffers: exactly the strings handed to the widgets,
        # kept so headless tests can assert on the render path directly.
        self._conversation_lines: list[str] = []
        self._activity_lines: list[str] = []
        self._status_text = ""
        self._seen_turn_ids: set[str] = set()
        # v0.0.30 single-stream presentation state (pure render layer -- no engine
        # change). The live plan renders as ONE mounted block updated IN PLACE; the
        # active step + mode LED are the ONLY motion (activity-only, gated off in
        # "off" anim mode).
        self._plan_steps: list[dict] = []   # [{"instruction", "status"}], the live plan
        self._plan_block = None             # legacy alias; dock is #plan-dock (U2)
        self._plan_mode = resolve_plan_mode(None)
        self._plan_pinned_full = False      # user explicitly chose /plan full
        self._cost_warn_level = "normal"    # escalates with envelope thresholds
        self._model_router = None
        self._session_approvals: set[str] = set()  # U4 session allowlist
        self._tool_folds: dict[int, dict] = {}     # id -> {label, result, expanded}
        self._tool_fold_seq = 0
        self._route_pulse = False
        self._find_query = ""
        # The rendered stream rows (Rich Text / str), in order -- the headless mirror
        # of what the single stream shows (so tests can pin speaker/finding styling
        # without depending on Textual widget internals). The live plan lives in the
        # plan dock (U2), not in the stream mirror.
        self._stream_rendered: list = []
        self._stream_plain: list[str] = []  # plain text for /find (U6)
        self._spin_frame = 0                # active-step spinner frame
        self._spin_timer = None             # active while a step is executing
        self._led_on = True                 # the mode LED's breathing phase
        self._led_timer = None
        self._load_tui_prefs()

    # -- layout ---------------------------------------------------------------

    def _load_tui_prefs(self) -> None:
        """Apply durable ``tui.*`` prefs from config.json (U6)."""
        try:
            from relay.store import load_config

            cfg = load_config() or {}
            tui = cfg.get("tui") or {}
            anim = tui.get("animations")
            if anim is False or str(anim).lower() in ("0", "false", "off"):
                self._anim_mode = "off"
            plan = tui.get("plan_dock")
            if isinstance(plan, str) and plan.strip().lower() in PLAN_MODES:
                self._plan_mode = plan.strip().lower()
                self._plan_pinned_full = self._plan_mode == "full"
        except Exception:  # noqa: BLE001 -- prefs must never block launch
            pass

    def _save_tui_prefs(self) -> None:
        """Persist animations + plan dock mode into config.json."""
        try:
            from relay.config import default_config
            from relay.store import CONFIG_VERSION, load_config, save_config

            config = load_config() or default_config()
            config.setdefault("version", CONFIG_VERSION)
            tui = config.setdefault("tui", {})
            tui["animations"] = self._anim_mode != "off"
            tui["plan_dock"] = self._plan_mode
            save_config(config)
        except Exception as exc:  # noqa: BLE001
            self._write_activity(f"(could not save tui prefs: {exc.__class__.__name__})", dim=True)

    def compose(self) -> ComposeResult:
        with Container(id="welcome"):
            with Vertical(id="welcome-inner"):
                yield Static(self._welcome_brand(), id="brand")
                yield Static(self._greeting, id="greeting")
                yield Static(self._indicator_text, id="indicator")
                yield Static("esc to cancel  ·  ctrl+q to quit", id="hint")
        with Container(id="working"):
            # IDE cockpit (U2): status rail on top, plan dock + stream below.
            yield Static(id="status")
            with Horizontal(id="cockpit-body"):
                yield Static(id="plan-dock", classes="plan-dock")
                yield VerticalScroll(id="stream")
        yield Static(id="command-popover")
        yield PromptInput(id="prompt", placeholder=self._placeholder)

    def _welcome_brand(self) -> str:
        """Welcome hero wordmark; SVG mark is packaged under relay/assets/ (U6)."""
        return RELAY_WORDMARK

    def on_mount(self) -> None:
        # The model indicator is visible from launch, BEFORE the first message
        # (promoted on the welcome screen; mirrored into the status buffer too).
        self._update_status()
        self.query_one("#prompt", Input).focus()
        self.set_interval(_SYNC_INTERVAL_S, self._sync_transcript)
        # The mode LED breathes (the ONE sanctioned always-on motion); "off" mode is
        # fully motionless (no timer), consistent with the glitch animator's "off".
        if self._anim_mode != "off":
            self._led_timer = self.set_interval(_LED_INTERVAL_S, self._led_tick)
        self._play_startup()
        # Graceful first-run: if there's no usable config (no working role+key from
        # env OR config/auth), guide the user into setup rather than letting them
        # type a doomed goal. Offered-but-prominent -- escapable, and a user with
        # working env vars/keys (the developer's state) never sees it.
        if not self._has_usable_config():
            self.call_after_refresh(self._enter_first_run_setup)

    def _has_usable_config(self) -> bool:
        """Whether a run could actually start: both roles resolve to a provider with
        an available key (env var OR stored auth.json). An injected client (tests)
        counts as a working backend."""
        if self._client is not None:
            return True
        for role in ROLES:
            provider = self._models.provider_for_role(role)
            try:
                profile = resolve_provider(provider)
            except ValueError:
                return False
            if resolve_key(profile.id, profile.key_env) is None:
                return False
        return True

    def _enter_first_run_setup(self) -> None:
        """Empty-state: teach the slash surface (the primary control plane), then
        open setup as a fallback. Offered-but-prominent + escapable; a user with
        working env vars/keys never reaches here."""
        self._first_run = True
        try:
            self.query_one("#hint", Static).update(
                "Type  /key  to add a provider key and get started  ·  "
                "/help  for all commands  ·  or set RELAY_* env vars"
            )
        except Exception:  # noqa: BLE001 -- hint not present
            pass
        self.action_open_setup()  # fallback: also open the full setup screen

    # -- startup + handoff animations (the look layer) -------------------------

    def _play_startup(self) -> None:
        """Boot glitch that resolves into the RELAY wordmark (short, non-blocking)."""
        self._play_glitch(
            self.query_one("#brand", Static), RELAY_WORDMARK,
            direction="in", duration=_STARTUP_SHORT_S,
        )

    def _play_transition(self) -> None:
        """Datamosh the welcome hero apart, then reveal the working panes."""
        self._play_glitch(
            self.query_one("#brand", Static), RELAY_WORDMARK,
            direction="out", duration=_TRANSITION_SHORT_S, on_done=self._show_working,
        )

    def _play_glitch(self, widget, target, *, direction, duration, on_done=None) -> None:
        """THE one place animations play; the mode gates the whole effect.

        ``"off"`` resolves instantly (no timers); ``"short"`` runs the datamosh;
        ``"long"`` is stubbed to short for now. Always non-blocking -- input is
        never gated on an animation; the run (if any) has already started.
        """
        self._stop_anim()
        lines = _normalize_block(target)
        final = "\n".join(lines) if direction == "in" else ""
        if self._anim_mode == "off":
            widget.update(final)
            if on_done is not None:
                on_done()
            return
        frames = max(2, int(duration * _ANIM_FPS))
        thresholds = _glitch_thresholds(lines)
        shimmer = random.Random()
        counter = {"frame": 0}

        def tick() -> None:
            if self._quitting:
                self._stop_anim()
                return
            counter["frame"] += 1
            progress = counter["frame"] / frames
            try:
                widget.update(glitch_frame(lines, thresholds, progress, shimmer, direction=direction))
                if counter["frame"] >= frames:
                    self._stop_anim()
                    widget.update(final)
                    if on_done is not None:
                        on_done()
            except Exception:  # noqa: BLE001 -- widget gone mid-animation; drop it
                self._stop_anim()

        self._anim_timer = self.set_interval(1 / _ANIM_FPS, tick)

    def _stop_anim(self) -> None:
        timer = self._anim_timer
        if timer is not None:
            try:
                timer.stop()
            except Exception:  # noqa: BLE001 -- already stopped/torn down
                pass
            self._anim_timer = None

    # -- the input box (one box, routed by engine state) -----------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        """Drive the slash popover from the prompt's text (dialog filters are
        handled on their own screens, so guard by id)."""
        if event.input.id != "prompt":
            return
        value = event.value
        # The popover opens whenever the engine is NOT actively generating: idle AND
        # the awaiting-user states (react / decide / approve) all accept a slash
        # command; only active planning/execution suppresses it (see _slash_allowed).
        if value.startswith("/") and self._slash_allowed():
            self._popover_update(value)
        else:
            self._popover_close()

    def _slash_allowed(self) -> bool:
        """Whether the `/` popover may open in the current router state.

        True unless the engine is ACTIVELY generating: idle (start a goal) and the
        states where the engine is WAITING ON THE USER (awaiting reaction / decision /
        approval) all permit slash commands; only active planning/execution suppresses
        the popover. This governs the popover ONLY -- routing, the engine, the bridge,
        and the InputRouter are unchanged, and a normal goal is unaffected in every
        state. (Input queueing is a separate future milestone, not this gate.)
        """
        return self._router.state not in _GENERATING_STATES

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "prompt":
            return  # a dialog's own field; its screen handles submit
        # Unified slash dispatch: any Command with accepts_args + a non-empty arg
        # runs immediately (same path for /queue, /redirect, /model, /cwd, …).
        inline = _parse_inline_command(event.value)
        if inline is not None:
            name, arg = inline
            command = command_by_name(name)
            if command is not None and command.accepts_args and arg:
                event.input.value = ""
                self._popover_close()
                command.run(self, arg)
                self._update_status()
                return
        # Enter while the popover is open runs the highlighted command, never a goal.
        if self._popover_open:
            if not self._popover_commands:
                # Unknown/partial slash: keep the typed text and give feedback
                # instead of silently clearing the prompt.
                self._write_activity("(unknown command)", dim=True)
                return
            event.input.value = ""
            self._popover_run_selected()
            return
        text = event.value
        event.input.value = ""
        outcome = self._router.submit(text)
        if outcome.action == ACTION_START:
            if self._view == "welcome":
                self._begin_first_run(text)
            else:
                self._start_run(text)
        elif outcome.action == ACTION_STEER:
            # Bare-interrupt-then-type: redirect now by replanning the remainder.
            self._start_steer(text)
        elif outcome.action == ACTION_STOP:
            # Empty submit at the interrupt prompt: abandon the plan, keep the session.
            self._stop_from_interrupt()
        elif outcome.action == ACTION_ANSWER:
            # Answers that become transcript turns render via the sync pass;
            # approval answers never reach the transcript, so echo them here.
            if outcome.kind == REQUEST_APPROVAL:
                self._write_conversation(f"you (approval): {text}", speaker="user")
        elif text.strip():
            self._write_activity("(input ignored: the engine is busy)")
        self._update_status()

    # -- the slash-command popover ---------------------------------------------

    def _popover_update(self, value: str) -> None:
        """Open/refresh the popover for prompt text ``value`` (starts with ``/``)."""
        self._popover_commands = filter_commands(self, value[1:])
        self._popover_index = 0
        self._popover_open = True
        popover = self.query_one("#command-popover", Static)
        popover.display = True
        popover.update(self._popover_text())

    def _popover_move(self, delta: int) -> None:
        if not self._popover_commands:
            return
        self._popover_index = max(
            0, min(len(self._popover_commands) - 1, self._popover_index + delta)
        )
        self.query_one("#command-popover", Static).update(self._popover_text())

    def _popover_close(self) -> None:
        if not self._popover_open:
            return
        self._popover_open = False
        self._popover_commands = []
        self._popover_index = 0
        try:
            self.query_one("#command-popover", Static).display = False
        except Exception:  # noqa: BLE001 -- not mounted
            pass

    def _popover_run_selected(self) -> None:
        """Run the highlighted command (Enter). Closes the popover; the command's
        run() opens its dialog. Never submits a goal."""
        commands = self._popover_commands
        index = self._popover_index
        self._popover_close()
        if commands and 0 <= index < len(commands):
            commands[index].run(self)

    def _popover_text(self) -> Text:
        text = Text()
        if not self._popover_commands:
            text.append("(no matching commands)", style="dim")
            return text
        for i, command in enumerate(self._popover_commands):
            marker = "> " if i == self._popover_index else "  "
            style = "reverse" if i == self._popover_index else ""
            text.append(f"{marker}/{command.name}", style=style)
            text.append(f"  -  {command.description}", style="dim")
            if i != len(self._popover_commands) - 1:
                text.append("\n")
        return text

    def _begin_first_run(self, goal: str) -> None:
        """First goal of the session: hand off welcome -> working, then run.

        The run kicks off IMMEDIATELY (never gated on the animation); the
        datamosh is a purely visual handoff that reveals the panes when it ends.
        """
        self._view = "working"
        self._stop_anim()  # stop the startup boot if it is still resolving
        self._start_run(goal)
        self._play_transition()

    def _show_working(self) -> None:
        """Swap the welcome screen for the working panes (the visual handoff)."""
        self.query_one("#welcome").display = False
        self.query_one("#working").display = True

    def _start_run(self, goal: str) -> None:
        self._goal_cost = 0.0  # a new goal: zero the per-goal counter (session untouched)
        self._cost_pulse = False
        self._stopping = False
        self._interrupting = False
        self._reset_plan()  # a new goal -> a fresh live plan (prior plan stays in scroll-back)
        # The transcript is SESSION-owned and accumulates across runs (its turn ids are
        # unique forever), so we do NOT clear _seen_turn_ids here -- only /clear does.
        self._session.goal = goal
        self._session.history.add(goal)
        if self._conversation_lines:
            self._write_conversation("")  # a blank line between runs
        self._write_conversation(f"you (goal): {goal}")
        self._router.begin_run()
        # Attach the session ModelRouter so planning/execution honor call-class.
        # Skip when tests inject a fake client (vendor/* models must stay unbound).
        if self._client is None and self._run_kwargs.get("model_router") is None:
            router = self._ensure_model_router()
            if router is not None:
                self._run_kwargs["model_router"] = router
        # Thread the SESSION-sticky working dir + the session transcript/memory, so a
        # working dir established earlier persists AND a later steer/queue continuation
        # keeps the same conversation + learnings.
        self._runner = EngineRunner(
            str(self._session.working_dir),
            models=self._models,
            client=self._client,
            assumption_level=self._assumption_level,
            auto_approve=self._auto_approve,
            on_request=self._marshal(self._handle_request),
            on_event=self._marshal(self._handle_event),
            on_finished=self._marshal(self._handle_finished),
            run_kwargs=self._run_kwargs,
            transcript=self._session.transcript,
            memory=self._session.memory,
            catalog=self._catalog,
        )
        self._runner.start(goal)

    def _start_steer(self, steer: str) -> None:
        """Apply a steer: replan the remainder of the last plan with ``steer`` folded
        in, then resume on the revision (same session). Counts as a plan revision.

        With no plan to continue (interrupted during planning), a steer is just a
        fresh redirection -- start a new run with the steer as the goal."""
        prior = self._session.last_plan
        if prior is None or not getattr(prior, "steps", None):
            self._start_run(steer)  # nothing to replan: treat as a fresh direction
            return
        max_revisions = self._run_kwargs.get("max_plan_revisions", 5)
        if not self._session.can_steer(max_revisions):
            self._write_activity(
                f"(steer refused: plan-revision budget {max_revisions} reached)"
            )
            self._router.finish_run()
            self._update_status()
            return
        self._session.note_steer()
        self._goal_cost = 0.0
        self._cost_pulse = False
        self._stopping = False
        self._interrupting = False
        self._reset_plan()  # the continuation replan emits a fresh plan to render
        self._session.history.add(steer)
        self._write_conversation(f"you (steer): {steer}")
        self._router.begin_run()
        self._runner = EngineRunner(
            str(self._session.working_dir),
            models=self._models,
            client=self._client,
            assumption_level=self._assumption_level,
            auto_approve=self._auto_approve,
            on_request=self._marshal(self._handle_request),
            on_event=self._marshal(self._handle_event),
            on_finished=self._marshal(self._handle_finished),
            run_kwargs=self._run_kwargs,
            transcript=self._session.transcript,
            memory=self._session.memory,
            catalog=self._catalog,
        )
        self._runner.start_steer(self._session.goal or steer, prior, steer)

    # -- worker -> UI marshaling (the only crossing) ----------------------------

    def _marshal(self, handler):
        """Wrap a UI handler so bridge callbacks (worker thread) reach it safely.

        Exceptions are logged and surfaced in the activity feed -- never silently
        dropped (a silent ``on_finished`` loss used to strand the UI).
        """

        def callback(*args) -> None:
            if self._quitting:
                return  # shutting down: drop UI updates, let the worker unwind
            try:
                self.call_from_thread(handler, *args)
            except Exception as exc:  # noqa: BLE001 -- app torn down OR handler bug
                _LOG.exception("marshal %s failed: %s", getattr(handler, "__name__", handler), exc)
                try:
                    self.call_from_thread(self._note_marshal_error, handler, exc)
                except Exception:  # noqa: BLE001 -- fully torn down; nothing left to tell
                    pass

        return callback

    def _note_marshal_error(self, handler, exc: BaseException) -> None:
        name = getattr(handler, "__name__", type(handler).__name__)
        self._write_activity(
            f"(ui marshal error in {name}: {exc.__class__.__name__})",
            dim=True,
        )

    def _handle_request(self, request: UiRequest) -> None:
        """A blocking ask arrived: show it, point the input box at it.

        Approvals open a dedicated modal (U4) instead of y/n in the goal box.
        """
        self._router.on_request(request)
        self._sync_transcript()
        if request.kind == REQUEST_APPROVAL:
            self._open_approve_modal(request)
            self._update_status()
            return
        if request.kind != REQUEST_REACTION:
            last_turn_text = self._last_synced_turn_text()
            if request.prompt.strip() != (last_turn_text or "").strip():
                for line in present_prompt(request.prompt).splitlines():
                    self._write_conversation(
                        f"brain: {line}" if line.strip() else "",
                        speaker="brain" if line.strip() else None,
                    )
        self._update_status()

    def _open_approve_modal(self, request: UiRequest) -> None:
        """Parse command/reason from the approval prompt and open ApproveDialog."""
        prompt = request.prompt or ""
        command = ""
        reason = ""
        for line in prompt.splitlines():
            s = line.strip()
            if s.startswith("Why gated:"):
                reason = s[len("Why gated:"):].strip()
            elif s and not s.startswith("The executor") and not s.startswith("Approve?"):
                if not command and not s.startswith("Why"):
                    command = s
        # Session allowlist short-circuit.
        if command and command in self._session_approvals:
            request.deliver("yes")
            self._write_activity(f"[approve] session-allow · {command[:80]}", dim=True)
            return

        def on_decision(action: str, req=request, cmd=command) -> None:
            # First settle wins (cancel already settled → deliver is a no-op).
            # Only mutate transcript / session allowlist when the answer landed.
            if action == "deny":
                if req.deliver("no"):
                    self._write_conversation("you (approval): no", speaker="user")
            else:
                if req.deliver("yes"):
                    if action == "session" and cmd:
                        self._session_approvals.add(cmd)
                    self._write_conversation(
                        f"you (approval): yes ({action})", speaker="user",
                    )
            self._update_status()

        self.push_screen(ApproveDialog(
            command=command or prompt[:200],
            reason=reason or "CONFIRM",
            on_decision=on_decision,
        ))

    def _handle_event(self, event: Event) -> None:
        """One engine event: phase changes steer the router; everything else renders
        INLINE in the single live stream (conversation, the live plan, tool calls,
        findings, verdicts), interleaved in the order it happens.

        Everything shown here is read from the event the engine ALREADY emitted --
        the render path makes no model call (proven by the zero-new-tokens guard).
        """
        if event.kind == EVENT_PHASE:
            self._router.set_phase(event.payload.get("phase", ""))
            self._pulse_route_if_anim()  # phase change = instrument tick when anim on
        elif event.kind == "route_change":
            self._on_route_change(event)
        else:
            self._render_event(event)
        self._sync_transcript()
        self._refresh_cost()
        self._update_status()

    def _on_route_change(self, event: Event) -> None:
        """U5: brief route chip pulse + activity note (zero tokens)."""
        payload = event.payload or {}
        name = payload.get("route") or payload.get("name") or "?"
        self._write_activity(f"[route] → {name}", dim=True)
        router = self._ensure_model_router()
        if router is not None and payload.get("route"):
            try:
                from relay.router import get_route, builtin_contract
                # Best-effort: refresh contract name display via router if API allows.
                _ = get_route(str(payload.get("route")))
            except Exception:  # noqa: BLE001
                pass
        self._pulse_route_if_anim()

    def _pulse_route_if_anim(self) -> None:
        if self._anim_mode == "off":
            return
        self._route_pulse = True
        try:
            self.set_timer(0.35, self._end_route_pulse)
        except Exception:  # noqa: BLE001
            self._route_pulse = False

    def _end_route_pulse(self) -> None:
        self._route_pulse = False
        self._update_status()

    # Event kinds that get a BESPOKE inline form in the stream (the live plan, tool
    # calls, findings, verdicts), so they are NOT also rendered as a generic speaker
    # row. Their attributed buffer line is still recorded (tests/debug-log contract).
    _SPECIAL_EVENTS = frozenset({
        "plan_proposed", "plan_created", "plan_revised", "replanned",
        "step_start", "step_done", "step_failed", "step_reviewed",
        "exec_action", "hands_finding",
    })

    def _render_event(self, event: Event) -> None:
        """Record the event into the activity/conversation BUFFERS (unchanged
        strings -- the test/debug contract) AND render its inline stream FORM."""
        kind = event.kind
        payload = event.payload or {}
        actor, line = describe_event_for_activity(event)

        # 1) Buffers: the attributed feed line. Special kinds record buffer-only (their
        #    visual is the bespoke inline form below); the rest get a generic stream row.
        if line:
            if kind in self._SPECIAL_EVENTS:
                self._record_activity(actor, line)
            else:
                self._write_activity(line, actor=actor)
        if kind == "exec_action":
            observation = " ".join((payload.get("observation") or "").split())
            if observation:
                self._record_activity(None, f"    {observation[:200]}")
        self._render_plan_split_buffer(payload)

        # 2) The inline stream forms (the v0.0.30 visual): the live plan updates IN
        #    PLACE; tool calls / findings / verdicts render as compact stream lines.
        if kind in ("plan_proposed", "plan_created", "plan_revised", "replanned"):
            steps = payload.get("steps")
            if isinstance(steps, list) and steps:
                self._plan_set([str(s) for s in steps],
                               revised=kind in ("plan_revised", "replanned"))
        elif kind == "step_start":
            self._plan_mark(payload.get("index"), "active")
        elif kind == "step_done":
            self._plan_mark(payload.get("index"), "done")
        elif kind == "step_failed":
            self._plan_mark(payload.get("index"), "failed")
        elif kind == "step_reviewed":
            self._stream_verdict(payload.get("index"), str(payload.get("verdict", "")))
        elif kind == "exec_action":
            self._stream_tool(line, " ".join((payload.get("observation") or "").split()))
        elif kind == "hands_finding":
            self._stream_finding(str(payload.get("finding", line)))

    def _render_plan_split_buffer(self, payload: dict) -> None:
        """The dual-fidelity split, BUFFER side (unchanged from v0.0.15): numbered
        executor **steps** -> the activity buffer; surfaced **assumptions** (the
        ``<assume>`` items) -> the conversation (buffer + a brain stream row). The
        live plan WIDGET is built separately (``_plan_set``); this only keeps the
        record the tests + the /log debug bundle assert on. Nothing is regenerated."""
        steps = payload.get("steps")
        if isinstance(steps, list) and steps:
            for i, step in enumerate(steps, 1):
                self._record_activity(None, f"    {i}. {step}")
        assumptions = payload.get("assumptions")
        if isinstance(assumptions, list) and assumptions:
            for assumption in assumptions:
                self._write_conversation(f"brain (assumes): {assumption}", speaker="brain")

    def _handle_finished(self, outcome: RunOutcome) -> None:
        self._sync_transcript()  # the result turn is in the transcript by now
        # The run has ended: settle the live plan so NO motion continues while the
        # engine is idle/awaiting you. A run that halted mid-step (esc-interrupt,
        # error, escalation limit, ...) leaves a step "active"; stop its spinner and
        # demote it to pending so the block shows a static resting state (every
        # terminal branch below flows through here, incl. the interrupt fork).
        self._settle_plan()
        cost = self._runner.ledger.total_cost() if self._runner is not None else None
        cost_note = "" if cost is None else f" (cost ${cost:.4f})"
        self._write_activity(f"[finished] {outcome.status}{cost_note}")
        # Two-tier cost: fold the goal's final cost into the session cumulative BEFORE
        # any branch, so an interrupted run's spend is preserved in the session tally.
        if cost is not None:
            self._goal_cost = cost
            self._session_cost += cost

        # The INTERRUPT fork: the user pressed esc and the run halted cleanly. Do NOT
        # finish the run to IDLE -- enter the interrupt prompt (session fully intact),
        # capturing the plan-so-far so a steer can replan its remainder.
        if self._interrupting and outcome.status == STATUS_CANCELLED:
            self._interrupting = False
            self._stopping = False
            self._session.last_plan = outcome.result.plan if outcome.result is not None else None
            # A /redirect issued mid-run queued a pending steer: apply it now (the run
            # has reached the clean boundary), instead of waiting at the interrupt prompt.
            pending = self._pending_steer
            self._pending_steer = None
            if pending is not None:
                self._start_steer(pending)
                return
            self._router.interrupt()
            self._write_activity("[interrupted] type to redirect, or esc again to stop")
            self._update_status()
            return

        if outcome.status == STATUS_ERROR:
            detail = friendly_provider_error(outcome.error)  # never leak raw API JSON
            self._write_conversation(f"brain (error): the run failed -- {detail}")
        elif outcome.result is None:
            # No execution happened (declined, or cancelled mid-conversation),
            # so no result turn exists; close the thread visibly anyway.
            self._write_conversation(f"(run ended: {outcome.status}; nothing was executed)")
        self._stopping = False  # the stop landed (or the run ended on its own)
        self._interrupting = False
        self._router.finish_run()
        # A COMPLETED run may have established a new working dir; adopt it so it
        # persists for the next goal. A cancelled/declined/errored run reports nothing
        # adoptable, so a cwd change that lived only in a cancelled plan never persists.
        if self._session.adopt_from_outcome(outcome):
            self._announce_working_dir(established=False)
        # Queue consumption: a clean completion picks up the next queued input (FIFO)
        # as the next direction WITHIN the same session (same cwd/memory/cost).
        if self._consume_queue():
            return
        self._update_status()

    def _do_queue(self, text: str) -> None:
        """`/queue <input>`: hold the input; the current step is NOT interrupted. When
        the current run completes it is consumed next (FIFO), as a new direction within
        the same session."""
        text = text.strip()
        if not text:
            return
        self._session.queue.enqueue(text)
        self._session.history.add(text)  # recallable via up-arrow
        self._write_activity(f"[queued] {text}  (queued: {len(self._session.queue)})")
        # An idle queue with no run in flight should start consuming immediately.
        if not self._run_in_flight():
            self._consume_queue()

    def _do_redirect(self, text: str) -> None:
        """`/redirect <input>`: steer NOW (the explicit form of bare-interrupt-then-type).

        Interrupted -> steer immediately. Running -> interrupt, then steer the moment
        the run halts at the clean boundary (a pending steer). Idle -> a fresh run."""
        text = text.strip()
        if not text:
            return
        if self._router.state is InputState.INTERRUPTED:
            self._start_steer(text)
        elif self._runner is not None and self._runner.is_running:
            self._pending_steer = text
            self._interrupting = True
            self._stopping = True
            self._runner.cancel()
            self._write_activity(f"[redirect] halting to steer: {text}")
        else:
            self._start_run(text)

    def _consume_queue(self) -> bool:
        """If the queue is non-empty, dequeue the next input and start it as the next
        run within the same session. Returns True when a queued item was started."""
        nxt = self._session.queue.dequeue()
        if nxt is None:
            return False
        self._write_activity(f"[queue] starting next queued input ({len(self._session.queue)} left)")
        self._start_run(nxt)
        return True

    # -- up-arrow recall: ONE unified recall-and-edit affordance ----------------

    def _recall_older(self) -> str | None:
        """Recall the previous input (goal/steer/queued) into the prompt for editing."""
        return self._session.history.recall_older()

    def _recall_newer(self) -> str | None:
        """Recall the next (newer) input; '' once stepped past the newest."""
        return self._session.history.recall_newer()

    # -- conversation pane: rendered from the Transcript ------------------------

    def _sync_transcript(self) -> None:
        """Append transcript turns not yet rendered (id-deduplicated, in order).

        Primary delivery is event/request/finished marshals; the slow interval is
        only a safety net. Reads go through :meth:`Transcript.snapshot_turns` so
        the UI thread never walks a list the worker is appending to.
        """
        runner = self._runner
        if runner is None:
            return
        transcript = runner.transcript
        if hasattr(transcript, "snapshot_turns"):
            snapshot = transcript.snapshot_turns()
        else:
            snapshot = list(getattr(transcript, "turns", []) or [])
        for turn in snapshot:
            if turn.id in self._seen_turn_ids:
                continue
            self._seen_turn_ids.add(turn.id)
            text = format_turn(turn)
            if turn.speaker != "user":
                text = present_prompt(text)
            speaker = "user" if turn.speaker == "user" else "brain"
            self._write_conversation(text, speaker=speaker)

    def _last_synced_turn_text(self) -> str | None:
        runner = self._runner
        if runner is None:
            return None
        transcript = runner.transcript
        if hasattr(transcript, "snapshot_turns"):
            turns = transcript.snapshot_turns()
        else:
            turns = list(getattr(transcript, "turns", []) or [])
        if not turns:
            return None
        return turns[-1].text

    # -- widget writes (the render path; buffers mirror the widgets for tests) --

    # The two logical buffers (_conversation_lines / _activity_lines) stay DISTINCT
    # -- the engine's brain<->hands split is still recorded for tests + the /log
    # bundle -- but both feed the ONE stream widget (interleaved in call order), so
    # there is no second pane. Untrusted content (tool output, model text) is built
    # via ``Text.append`` so it is never parsed as console markup.

    def _stream(self):
        """The stream container (None when not mounted -- logic-only construction)."""
        try:
            return self.query_one("#stream", VerticalScroll)
        except Exception:  # noqa: BLE001 -- not mounted
            return None

    def _mount_stream(self, widget) -> None:
        """Mount one row/widget into the stream; follow the live edge only if pinned."""
        stream = self._stream()
        if stream is None:
            return
        try:
            follow = stream_should_follow(stream)
            stream.mount(widget)
            trim_stream_children(stream, keep=self._plan_block, max_lines=STREAM_MAX_LINES)
            if follow:
                stream.scroll_end(animate=False)
        except Exception:  # noqa: BLE001 -- teardown race; the buffer already has it
            pass

    def _push_row(self, renderable, *, classes: str = "stream-row", plain: str | None = None) -> None:
        """Record a stream row (the headless mirror) and mount it into the stream."""
        self._stream_rendered.append(renderable)
        trim_deque_list(self._stream_rendered, STREAM_MAX_LINES)
        plain_text = plain
        if plain_text is None:
            plain_text = renderable.plain if hasattr(renderable, "plain") else str(renderable)
        self._stream_plain.append(plain_text)
        trim_deque_list(self._stream_plain, STREAM_MAX_LINES)
        self._mount_stream(Static(renderable, classes=classes))

    def _row(self, gutter: str, body: str, *, gutter_style: str = "", body_style: str = "") -> None:
        """Build + push one labeled stream row (the mockup's gutter + body line)."""
        body_renderable = render_conversation_body(body) if gutter == "brain" else body
        if not isinstance(body_renderable, str):
            # Markdown / structured: gutter as a prefix line, then the body widget.
            head = Text()
            head.append(f"{gutter:<6}" if gutter else " " * 6, style=gutter_style or C_DIM)
            self._push_row(head, plain=f"{gutter} {body}")
            self._push_row(body_renderable, plain=body)
            return
        text = Text()
        text.append(f"{gutter:<6}" if gutter else " " * 6, style=gutter_style or C_DIM)
        text.append(body_renderable, style=body_style or C_TXT)
        self._push_row(text, plain=f"{gutter} {body}".strip())

    def _record_activity(self, actor: str | None, line: str) -> None:
        """Append to the activity BUFFER only (the test/debug record) -- no stream row.
        Used for the bespoke-form events + the dim detail lines, so the stream shows
        their inline form (or nothing) rather than a duplicate generic row."""
        self._activity_lines.append(f"{actor} | {line}" if actor else line)
        trim_deque_list(self._activity_lines, STREAM_BUFFER_MAX)

    def _write_conversation(self, line: str, *, speaker: str | None = None) -> None:
        """Record a conversation line (buffer) and render it as a stream row.

        Prefer a structured ``speaker`` (``user`` / ``brain`` / other) over
        re-parsing the rendered line. Legacy callers may omit it.
        """
        self._conversation_lines.append(line)
        trim_deque_list(self._conversation_lines, STREAM_BUFFER_MAX)
        if not line.strip():
            self._push_row("")  # a blank spacer between runs
            return
        if speaker == "user" or (speaker is None and line.startswith("you")):
            rest = line.split(None, 1)[1] if " " in line else ""
            self._row("you", rest, gutter_style=f"bold {W_TEXT}", body_style=C_TXT)
        elif speaker == "brain" or (speaker is None and line.startswith("brain")):
            rest = line.split(None, 1)[1] if " " in line else ""
            self._row("brain", rest, gutter_style=W_RED, body_style=C_TXT)
        else:
            self._row("", line, body_style=C_MUTED)  # system / result / notice lines

    def _write_activity(self, line: str, *, actor: str | None = None, dim: bool = False) -> None:
        """Record an activity line (buffer) and render it inline in the stream.

        ``actor`` (brain/hands/you) renders a colored speaker row; an actor-less line
        is a muted (or dim) system note. Event-driven detail lines are recorded via
        :meth:`_record_activity` instead, so they never double-render in the stream."""
        self._record_activity(actor, line)
        if actor:
            self._row(actor, line, gutter_style=_ACTOR_STYLES.get(actor, ""),
                      body_style=C_MUTED if actor == ACTOR_HANDS else C_TXT)
        else:
            self._row("", line, body_style=C_DIM if dim else C_MUTED)

    # -- the inline forms: tool calls, findings, verdicts (hands acting) --------

    def _stream_tool(self, label: str, result: str = "") -> None:
        """A compact tool-call stream line; long bodies fold by default (U3)."""
        fold_id = self._tool_fold_seq
        self._tool_fold_seq += 1
        expanded = False
        self._tool_folds[fold_id] = {
            "label": label, "result": result, "expanded": expanded,
        }
        text = tool_summary_line(label, result, folded=bool(result) and len(result) > 60)
        obs = render_observation(result, expanded=False) if result else None
        self._push_row(text, plain=f"{label} {result}", classes=f"stream-row tool-{fold_id}")
        if obs is not None and not isinstance(obs, str):
            # Diff-shaped: show a folded syntax preview beneath the summary.
            self._push_row(obs, plain=result[:200], classes=f"stream-row tool-body-{fold_id}")

    def _toggle_tool_fold(self, fold_id: int) -> None:
        """Expand/collapse a folded tool observation (click or /expand)."""
        meta = self._tool_folds.get(fold_id)
        if not meta:
            return
        meta["expanded"] = not meta["expanded"]
        self._write_activity(
            f"[tool] {'expanded' if meta['expanded'] else 'folded'} · {meta['label']}",
            dim=True,
        )
        if meta["expanded"] and meta.get("result"):
            body = render_observation(meta["result"], expanded=True)
            self._push_row(body, plain=meta["result"])

    def _stream_finding(self, note: str) -> None:
        """A finding (v0.0.29 hands->brain channel) renders as a distinct GREEN line."""
        text = Text()
        text.append("  ⚠ finding", style=f"bold {C_GREEN}")
        text.append(f" → {note}", style=C_MUTED)
        self._push_row(text)

    def _stream_verdict(self, index, verdict: str) -> None:
        """A compact review verdict line: ``review ✓ accept · step 04``."""
        # Classify from the payload field (exact match), not substring search.
        accepted = (verdict or "").strip().lower() == "accept"
        text = Text()
        text.append("  review ", style=C_DIM)
        text.append(f"{'✓' if accepted else '•'} {verdict}", style=C_GREEN if accepted else C_AMBER)
        try:
            text.append(f"  · step {int(index) + 1:02d}", style=C_DIM)
        except (TypeError, ValueError):
            pass
        self._push_row(text)

    # -- the inline LIVE plan: ONE block, updated IN PLACE -----------------------

    def _plan_set(self, steps: list[str], *, revised: bool = False) -> None:
        """(Re)build the live plan and refresh the plan dock (U2 source of truth).

        A compact ``plan committed`` line goes to the stream; the dock holds the
        interactive step list with active highlight.
        """
        if revised and self._plan_steps:
            kept = [s for s in self._plan_steps if s["status"] != "pending"]
            self._plan_steps = kept + [{"instruction": s, "status": "pending"} for s in steps]
        else:
            self._plan_steps = [{"instruction": s, "status": "pending"} for s in steps]
            self._write_activity(
                f"plan committed · {len(steps)} step{'s' if len(steps) != 1 else ''}",
                dim=True,
            )
        self._plan_render()

    def _plan_mark(self, index, status: str) -> None:
        """Mark the step at ``index`` (0-based engine index == list position) in place."""
        if index is None:
            return
        try:
            self._plan_steps[int(index)]["status"] = status
        except (IndexError, TypeError, ValueError):
            return
        if status == "active":
            self._start_spin()
        self._plan_render()

    def _plan_dock(self):
        try:
            return self.query_one("#plan-dock", Static)
        except Exception:  # noqa: BLE001
            return None

    def _effective_plan_mode(self) -> str:
        width = None
        try:
            width = self.size.width
        except Exception:  # noqa: BLE001
            width = None
        return resolve_plan_mode(
            self._plan_mode, width=width, pinned_full=self._plan_pinned_full,
        )

    def _refresh_plan_dock_visibility(self) -> None:
        dock = self._plan_dock()
        if dock is None:
            return
        mode = self._effective_plan_mode()
        try:
            if mode == "hidden":
                dock.add_class("-hidden")
            else:
                dock.remove_class("-hidden")
        except Exception:  # noqa: BLE001
            pass

    def _plan_render(self) -> None:
        """Render the plan dock from step states (full / active / hidden)."""
        dock = self._plan_dock()
        mode = self._effective_plan_mode()
        self._refresh_plan_dock_visibility()
        rendered = render_plan_dock(
            self._plan_steps, mode=mode, spin_frame=self._spin_frame,
        )
        self._plan_block = dock
        if dock is not None:
            try:
                dock.update(rendered)
            except Exception:  # noqa: BLE001
                pass
        if not any(s["status"] == "active" for s in self._plan_steps):
            self._stop_spin()

    def _set_plan_mode(self, mode: str) -> None:
        mode = (mode or "").strip().lower()
        if mode not in PLAN_MODES:
            self._write_activity(f"/plan: use full|active|hidden (got '{mode}')")
            return
        self._plan_mode = mode
        self._plan_pinned_full = mode == "full"
        self._plan_render()
        self._update_status()
        self._write_activity(f"[plan] dock mode = {mode}")
        self._save_tui_prefs()

    def _cmd_plan(self, arg: str = "") -> None:
        """Set plan dock mode: ``/plan full|active|hidden``."""
        mode = (arg or "").strip().lower()
        if not mode:
            self._write_activity(
                f"[plan] mode={self._effective_plan_mode()} "
                f"(session={self._plan_mode}; /plan full|active|hidden)"
            )
            return
        self._set_plan_mode(mode)

    def _cmd_anim(self, arg: str = "") -> None:
        """Session animation kill switch: ``/anim off|on`` (U5 surface)."""
        raw = (arg or "").strip().lower()
        if raw in ("off", "0", "false"):
            self._anim_mode = "off"
            self._stop_spin()
            if self._led_timer is not None:
                try:
                    self._led_timer.stop()
                except Exception:  # noqa: BLE001
                    pass
                self._led_timer = None
            self._write_activity("[anim] off — instant updates only")
            self._save_tui_prefs()
        elif raw in ("on", "1", "true", "short"):
            self._anim_mode = "short"
            if self._led_timer is None:
                try:
                    self._led_timer = self.set_interval(_LED_INTERVAL_S, self._led_tick)
                except Exception:  # noqa: BLE001
                    self._led_timer = None
            self._write_activity("[anim] on")
            self._save_tui_prefs()
        else:
            self._write_activity(f"[anim] mode={self._anim_mode} — /anim on|off")
        self._update_status()

    def _cmd_find(self, arg: str = "") -> None:
        """Search stream scrollback for ``query`` (U6)."""
        query = (arg or "").strip()
        if not query:
            self._write_activity("[find] usage: /find <text>")
            return
        self._find_query = query
        hits = find_in_lines(self._stream_plain, query)
        if not hits:
            # Also search conversation/activity buffers ( /log sources ).
            hits_buf = find_in_lines(self._conversation_lines + self._activity_lines, query)
            if not hits_buf:
                self._write_activity(f"[find] no matches for '{query}'")
                return
            self._write_activity(
                f"[find] {len(hits_buf)} match(es) in buffers · "
                f"first: { (self._conversation_lines + self._activity_lines)[hits_buf[0]][:80] }"
            )
            return
        first = hits[0]
        self._write_activity(
            f"[find] {len(hits)} match(es) · first#{first}: {self._stream_plain[first][:80]}"
        )
        stream = self._stream()
        if stream is not None:
            try:
                children = list(stream.children)
                if 0 <= first < len(children):
                    stream.scroll_to_widget(children[first], animate=self._anim_mode != "off")
            except Exception:  # noqa: BLE001
                pass

    def _reset_plan(self) -> None:
        """Drop the live-plan state (a new goal starts a fresh plan; prior plans stay
        frozen in scroll-back). Stops the active-step spinner."""
        self._plan_steps = []
        self._plan_block = None
        self._stop_spin()
        self._plan_render()

    def _settle_plan(self) -> None:
        """The run ended: stop the active-step spinner and demote any still-active step
        to pending (it never settled), so the plan block rests on a static icon and no
        motion continues while the engine is idle. Keeps the plan in scroll-back (unlike
        :meth:`_reset_plan`, which is for a NEW goal)."""
        demoted = False
        for step in self._plan_steps:
            if step["status"] == "active":
                step["status"] = "pending"
                demoted = True
        self._stop_spin()
        if demoted:
            self._plan_render()

    # -- activity-only motion: the active-step spinner + the mode LED ------------

    def _start_spin(self) -> None:
        """Start the active-step spinner (off entirely in "off" anim mode)."""
        if self._anim_mode == "off" or self._spin_timer is not None:
            return
        try:
            self._spin_timer = self.set_interval(_SPIN_INTERVAL_S, self._spin_tick)
        except Exception:  # noqa: BLE001 -- not mounted (logic-only use)
            self._spin_timer = None

    def _spin_tick(self) -> None:
        if self._quitting:
            self._stop_spin()
            return
        self._spin_frame += 1
        self._plan_render()

    def _stop_spin(self) -> None:
        timer = self._spin_timer
        if timer is not None:
            try:
                timer.stop()
            except Exception:  # noqa: BLE001 -- already stopped/torn down
                pass
            self._spin_timer = None

    def _led_tick(self) -> None:
        """Toggle the breathing mode LED and re-render the status line."""
        if self._quitting:
            return
        self._led_on = not self._led_on
        self._update_status()

    def _mode_word(self, state) -> str:
        return mode_word(state)

    def _step_segment(self) -> str:
        return step_segment(self._plan_steps)

    def _ensure_model_router(self):
        """Lazy ModelRouter for the status rail ``route=`` chip."""
        if self._model_router is not None:
            return self._model_router
        try:
            from relay.router import ModelRouter

            self._model_router = ModelRouter.from_resolve(
                None, root=self._session.working_dir,
            )
        except Exception:  # noqa: BLE001 -- rail must never crash
            self._model_router = None
        return self._model_router

    def _status_snapshot(self) -> StatusSnapshot:
        """Build the cockpit status facts (mirror + widget share this)."""
        state = self._router.state
        mode = mode_word(state)
        step = step_segment(self._plan_steps)
        instr = active_instruction(self._plan_steps)
        if step and instr:
            step = f"{step} · {instr}"
        runner = self._runner
        envelope = getattr(runner, "envelope", None) if runner is not None else None
        ledger = getattr(runner, "ledger", None) if runner is not None else None
        cost, cost_level = cost_segment(
            goal_cost=self._goal_cost,
            visible=self._cost_visible,
            run_in_flight=self._run_in_flight(),
            stopping=self._stopping,
            envelope=envelope,
            ledger=ledger,
        )
        if self._cost_pulse:
            cost_level = "pulse"
        if self._cost_warn_level in ("warn", "critical") and cost_level == "normal":
            cost_level = self._cost_warn_level
        route = route_segment(self._ensure_model_router())
        ctx = context_segment(self._models, self._catalog)
        cwd = self._cwd_segment()
        queued = f"queued: {len(self._session.queue)}" if self._session.queue else ""
        models = f"brain {self._models.brain} · hands {self._models.hands}"
        hint = (
            "esc interrupt · /queue"
            if self._run_in_flight()
            else "enter send · ↑ recall · /queue"
        )
        return StatusSnapshot(
            mode=mode, step=step, cost=cost, cost_level=cost_level,
            route=route, context=ctx, cwd=cwd, models=models, queued=queued, hint=hint,
        )

    def _update_status(self) -> None:
        """Status rail: LED · phase · step · cost · route · ctx · models · queue."""
        snap = self._status_snapshot()
        self._status_text = snap.plain()
        working = snap.mode == "WORKING"
        led_color = C_GREEN if working else W_RED
        text = Text()
        text.append("● " if self._led_on else "○ ", style=led_color)
        text.append(snap.mode, style=f"bold {led_color}")
        for seg, style in (
            (snap.step, W_TEXT),
            (snap.cost, self._cost_style(snap.cost_level)),
            (snap.route, f"bold {W_RED}" if self._route_pulse else C_DIM),
            (snap.context, C_DIM),
            (snap.cwd, W_TEXT_DIM),
            (snap.models, C_DIM),
            (snap.queued, W_RED),
        ):
            if not seg:
                continue
            text.append("  ·  ", style=C_DIM)
            text.append(seg, style=style)
        text.append("    ", style=C_DIM)
        text.append(snap.hint, style=C_DIM)
        try:
            self.query_one("#status", Static).update(text)
        except Exception:  # noqa: BLE001 -- not mounted / teardown race
            pass
        try:
            self.query_one("#prompt", Input).placeholder = placeholder_for_state(
                self._router.state, self._placeholder
            )
        except Exception:  # noqa: BLE001 -- not mounted
            pass
        self._refresh_plan_dock_visibility()

    def _cost_style(self, level: str) -> str:
        if level == "pulse":
            return f"bold {W_WARN}"
        if level == "critical":
            return f"bold {W_RED}"
        if level == "warn":
            return f"bold {W_WARN}"
        return W_WARN

    def _cwd_segment(self) -> str:
        """The status-line working-dir segment, shown when off the launch root."""
        session = self._session
        if session.is_launch_root():
            return ""
        try:
            label = session.working_dir.relative_to(session.launch_root).as_posix()
        except ValueError:
            label = session.working_dir.name
        return f"cwd={label}"

    def _announce_working_dir(self, *, established: bool) -> None:
        """Surface where Relay will work now (a visible notice). ``established`` is
        True for an explicit set, False when adopted from a completed run."""
        wd = self._session.working_dir
        line = f"working directory {'set' if established else 'now'}: {wd}"
        if self._view == "working":
            self._write_activity(line, actor=ACTOR_BRAIN)
        else:
            try:
                self.query_one("#hint", Static).update(line)
            except Exception:  # noqa: BLE001 -- hint not mounted
                pass

    # -- live cost: a two-tier counter (per-goal + session); reads ALREADY-tracked
    # cost off the run's ledger, so the whole path makes ZERO model calls. Relay
    # SHOWS spend and lets the user stop -- it never imposes a cap.

    def _run_in_flight(self) -> bool:
        """Whether a run is live (any non-idle router state) -- drives the 'esc to
        stop' affordance and whether the session rollup includes the live goal."""
        return self._router.state is not InputState.IDLE

    def _cost_segment(self) -> str:
        """Status-rail cost text (empty when hidden). Prefers envelope remaining."""
        text, _level = cost_segment(
            goal_cost=self._goal_cost,
            visible=self._cost_visible,
            run_in_flight=self._run_in_flight(),
            stopping=self._stopping,
            envelope=getattr(self._runner, "envelope", None) if self._runner else None,
            ledger=getattr(self._runner, "ledger", None) if self._runner else None,
        )
        return text

    def _session_total(self) -> float:
        """Session spend: folded finished goals plus the live current goal while a run
        is in flight (so it reflects the in-flight goal without double-counting once
        that goal is folded into ``_session_cost`` at finish)."""
        live = self._goal_cost if self._run_in_flight() else 0.0
        return self._session_cost + live

    def _refresh_cost(self) -> None:
        """Read the live per-goal cost off the active run's ledger. Cost is ALREADY
        tracked (telemetry), so this makes NO model call; a no-op when unchanged."""
        runner = self._runner
        if runner is None:
            return
        cost = runner.ledger.total_cost()
        envelope = getattr(runner, "envelope", None)
        if envelope is not None and hasattr(envelope, "drain_warnings"):
            for warn in envelope.drain_warnings(ledger=runner.ledger, steps_used=None):
                self._cost_warn_level = (
                    "critical" if float(warn.get("threshold") or 0) >= 0.99 else "warn"
                )
                self._write_activity(f"[envelope] {warn.get('message', 'warn')}", dim=True)
                self._flash_cost()
        if cost is None or cost == self._goal_cost:
            return
        self._goal_cost = cost
        self._flash_cost()  # transient highlight; the caller re-renders the status

    def _flash_cost(self) -> None:
        """Briefly highlight the counter when it climbs -- a single transient style
        flip reverted by a short timer (NOT an animation loop or thread). Pure
        presentation on the already-tracked figure: zero model calls."""
        self._cost_pulse = True
        timer = self._cost_pulse_timer
        if timer is not None:
            try:
                timer.stop()
            except Exception:  # noqa: BLE001 -- already stopped/torn down
                pass
        try:
            self._cost_pulse_timer = self.set_timer(_COST_PULSE_S, self._end_cost_pulse)
        except Exception:  # noqa: BLE001 -- not mounted (logic-only use in tests)
            self._cost_pulse_timer = None

    def _end_cost_pulse(self) -> None:
        self._cost_pulse = False
        self._cost_pulse_timer = None
        self._update_status()

    # -- the setup / picker flow ------------------------------------------------

    def action_open_setup(self) -> None:
        """Open the provider setup screen (key entry + per-role model picker)."""
        self.push_screen(
            SetupScreen(
                models=self._models,
                list_models_fn=self._list_models_fn,
                validate_fn=self._validate_fn,
                on_saved=self._on_setup_saved,
            )
        )

    def _on_setup_saved(self) -> None:
        """A setup save landed: re-resolve config so the LIVE app reflects it.

        The welcome model indicator + status line now show config.json selections,
        not just env. (A run already in flight keeps its own resolved models.) If an
        env var is shadowing the just-saved selection (env > config), the save has no
        visible effect -- so we surface an honest note rather than letting the screen
        look stale (see :meth:`_env_shadow_notice`).
        """
        self._models = load_models()
        self._indicator_text = model_identity(self._models)
        self._save_notice = self._env_shadow_notice()
        try:
            self.query_one("#indicator", Static).update(self._indicator_text)
        except Exception:  # noqa: BLE001 -- indicator not present (e.g. mid-working)
            pass
        self._render_save_notice()
        self._update_status()

    def _env_shadow_notice(self) -> str:
        """A one-line note when a saved model is being OVERRIDDEN by an env var.

        The save wrote config.json, but ``env > config`` means a ``RELAY_*_MODEL``
        env var (or a project ``.env``) wins -- so the change has no visible effect.
        Returns "" in the common case (nothing shadowed). ASCII-safe; this only
        REPORTS the shadow -- resolution precedence is unchanged.
        """
        overrides = [
            f"{env} is overriding your saved {role} model"
            for role in ROLES
            if (env := env_override_for(role, "model"))
        ]
        if not overrides:
            return ""
        return (
            "Saved to config.json -- but " + "; ".join(overrides)
            + " (unset it to use the saved value)."
        )

    def _render_save_notice(self) -> None:
        """Surface the shadow note where the user is looking -- the welcome hint line
        and (in the working view) the activity feed. A no-op when nothing is shadowed."""
        notice = self._save_notice
        if not notice:
            return
        try:
            self.query_one("#hint", Static).update(notice)
        except Exception:  # noqa: BLE001 -- hint not mounted (working view)
            pass
        if self._view == "working":
            self._write_activity(notice)

    # -- the slash commands (each opens a dialog or does a clean action) --------

    def _cmd_help(self) -> None:
        """List every command; selecting one runs it (the discoverability anchor)."""
        options = [
            {
                "title": f"/{c.name}  -  {c.title}",
                "value": c.name,
                "description": c.description,
                "category": c.category,
                "on_select": (lambda v, cmd=c: cmd.run(self)),
            }
            for c in visible_commands(self)
        ]
        self.push_screen(SelectDialog(title="Commands", options=options))

    def _cmd_model(self, arg: str = "") -> None:
        """Pick a role, then its model — or apply ``/model <role> [slug]`` inline."""
        parts = (arg or "").split(None, 1)
        if parts and parts[0] in ROLES:
            role = parts[0]
            if len(parts) == 2:
                model = parts[1].strip()
                provider = self._models.provider_for_role(role)
                ok, note = self._save_role_model(role, provider, model)
                self._write_activity(
                    f"/model {role} → {provider}/{model}" if ok else f"/model failed: {note}"
                )
                return
            self._pick_model_for(role)
            return
        options = [
            {"title": "brain (planner)", "value": "brain",
             "on_select": (lambda v: self._pick_model_for("brain"))},
            {"title": "hands (executor)", "value": "hands",
             "on_select": (lambda v: self._pick_model_for("hands"))},
        ]
        self.push_screen(SelectDialog(title="Set the model for which role?", options=options))

    def _pick_model_for(self, role: str) -> None:
        """/model's model step: pick a model for the role's CURRENT provider."""
        self._pick_model_step(role, self._models.provider_for_role(role))

    def _pick_model_step(self, role: str, provider: str, *, then=None) -> None:
        """The SHARED model-pick step (used by both /model and /provider).

        ``provider`` is explicit (so /provider can pick a model for a JUST-CHOSEN
        provider, not the stale config one). A ``list`` provider (DeepSeek) shows
        the live ``/models`` SelectDialog after an off-thread fetch; a ``manual``
        provider (OpenRouter) a slug TextEntryDialog validated live. On a successful
        save, ``then`` (if given) is scheduled AFTER this dialog tears down.
        """
        try:
            profile = resolve_provider(provider)
        except ValueError:
            profile = None

        def after_save(ok: bool) -> None:
            if ok and then is not None:
                self.call_after_refresh(then)  # next step, after this dialog dismisses

        if profile is not None and profile.discovery == DISCOVERY_LIST:
            self._pending_model_pick = {
                "role": role, "provider": provider, "then": then, "after_save": after_save,
            }
            self._write_activity(f"(loading {provider} models…)", dim=True)
            self.run_worker(
                self._fetch_models_for_pick,
                thread=True,
                name="list-models",
                group="list-models",
                exclusive=True,
                exit_on_error=False,
            )
        else:
            # manual aggregator: a slug field validated live before saving.
            # Off-thread only for the real network validator — injected test
            # seams stay sync so headless pilots can assert on submit().
            def on_submit(slug, r=role, p=provider):
                ok, note = self._save_role_model(r, p, slug)
                after_save(ok)
                return ok, note

            use_async = self._validate_fn is None or (
                self._validate_fn is provider_validate_model
            )
            self.push_screen(TextEntryDialog(
                title=f"{role} model ({provider})",
                label="Type a model slug (validated live before saving):",
                password=False, placeholder="e.g. openai/gpt-4o",
                on_submit=on_submit,
                async_submit=use_async,
            ))

    def _fetch_models_for_pick(self) -> list[str]:
        """Worker body: list models for the pending /model pick (may hit the network)."""
        pending = getattr(self, "_pending_model_pick", None) or {}
        provider = pending.get("provider", "")
        list_fn = self._list_models_fn or provider_list_models
        try:
            return list(list_fn(provider))
        except Exception:  # noqa: BLE001 -- no key/network -> empty, handled below
            return []

    def _show_model_pick_dialog(self, ids: list[str]) -> None:
        pending = getattr(self, "_pending_model_pick", None) or {}
        role = pending.get("role", "brain")
        provider = pending.get("provider", "")
        after_save = pending.get("after_save") or (lambda ok: None)
        self._pending_model_pick = None

        def on_pick(value, r=role, p=provider) -> None:
            ok, _ = self._save_role_model(r, p, value)
            after_save(ok)

        options = [
            {"title": mid, "value": mid, "category": provider, "on_select": on_pick}
            for mid in ids
        ] or [{"title": "(no models listed -- add a key with /key)", "value": "__none__"}]
        self.push_screen(SelectDialog(title=f"Pick a {role} model ({provider})", options=options))

    # -- /provider: set a role's provider, then its model ----------------------

    def _cmd_provider(self) -> None:
        """Pick a role (segmented toggle), then its provider, then its model.

        Reuses the provider SelectDialog (/key's list) and the SHARED model-pick
        step (/model's), plus persist_role -- no forked logic. Per-role isolation:
        the role chosen here is the ONLY role touched (``both`` runs the model step
        twice, brain then hands, each self-contained and each persisted).
        """
        options = [
            {"label": "brain", "value": "brain"},
            {"label": "hands", "value": "hands"},
            {"label": "both", "value": "both"},
        ]
        self.push_screen(SegmentedControl(
            title="Set the provider for which role?",
            options=options, start_index=0,
            on_select=(lambda scope: self._provider_choose_provider(scope)),
        ))

    def _provider_choose_provider(self, scope: str) -> None:
        """Step 2: pick the provider for the chosen role(s) -- the same provider
        SelectDialog /key and setup use (``known_providers()``)."""
        options = [
            {"title": pid, "value": pid,
             "on_select": (lambda p, s=scope: self._provider_set(s, p))}
            for pid in known_providers()
        ]
        self.push_screen(SelectDialog(title=f"Provider for {scope}", options=options))

    def _provider_set(self, scope: str, provider: str) -> None:
        """Step 3: chain into the model pick for the chosen provider. ``both`` runs
        the model step TWICE -- brain, then (on success) hands -- each persisted."""
        roles = ["brain", "hands"] if scope == "both" else [scope]
        self._provider_model_chain(roles, provider, 0)

    def _provider_model_chain(self, roles: list[str], provider: str, index: int) -> None:
        if index >= len(roles):
            return
        role = roles[index]
        self._pick_model_step(
            role, provider,
            then=(lambda: self._provider_model_chain(roles, provider, index + 1)),
        )

    def _save_role_model(self, role: str, provider: str, model: str, thinking=None) -> tuple[bool, str]:
        """Persist a role's model via the SHARED persist_role path, then live-reload.

        Returns ``(ok, note)`` so a TextEntryDialog can show the rejection inline.
        """
        if thinking is None:
            thinking = self._models.thinking_for_role(role)
        ok, note = _call_persist_role(
            role, provider, model, thinking, validate_fn=self._validate_fn or provider_validate_model
        )
        if ok:
            self._on_setup_saved()  # indicator/status reflect config.json now
        else:
            note = friendly_provider_error(note, provider=provider, model=model)
        return ok, note

    def _cmd_key(self) -> None:
        """Pick a provider, then enter its key in a MASKED dialog (never inline)."""
        options = [
            {"title": pid, "value": pid, "on_select": (lambda v: self._enter_key_for(v))}
            for pid in known_providers()
        ]
        self.push_screen(SelectDialog(title="Add a key for which provider?", options=options))

    def _enter_key_for(self, provider: str) -> None:
        self.push_screen(TextEntryDialog(
            title=f"API key for {provider}",
            label="Paste the key (hidden; never shown, logged, or in config.json):",
            password=True, placeholder="sk-...",
            on_submit=(lambda key, p=provider: self._save_key(p, key)),
        ))

    def _save_key(self, provider: str, key: str) -> tuple[bool, str]:
        """Store a key (from the masked dialog ONLY) to auth.json 0o600, then reload."""
        key = (key or "").strip()
        if not key:
            return False, "no key entered"
        _call_secrets_set_key(provider, key)  # the same v0.0.16 secrets path; value never echoed
        self._on_setup_saved()
        return True, f"stored a key for {provider}"

    def _open_inline_dialog(self, kind: str) -> None:
        """The popover entry for /redirect and /queue: a minimal single-field dialog
        (the inline `/redirect <input>` / `/queue <input>` form is the primary path;
        rich queue UI is deferred to the UI-overhaul milestone)."""
        title = "Redirect (steer now)" if kind == "redirect" else "Queue (do this next)"
        handler = self._do_redirect if kind == "redirect" else self._do_queue

        def on_submit(value: str) -> tuple[bool, str]:
            value = (value or "").strip()
            if not value:
                return False, "enter some input"
            handler(value)
            self._update_status()
            return True, "ok"

        self.push_screen(TextEntryDialog(
            title=title, label=f"Input to {kind}:", placeholder="...", on_submit=on_submit,
        ))

    def _cmd_queue(self, arg: str = "") -> None:
        """Queue input for after the current run (``/queue <text>`` or dialog)."""
        text = (arg or "").strip()
        if text:
            self._do_queue(text)
            return
        self._open_inline_dialog("queue")

    def _cmd_redirect(self, arg: str = "") -> None:
        """Steer now (``/redirect <text>`` or dialog)."""
        text = (arg or "").strip()
        if text:
            self._do_redirect(text)
            return
        self._open_inline_dialog("redirect")

    def _cmd_config(self) -> None:
        """Show the resolved config (provider/model/thinking + source; key present/
        absent) -- NEVER the key. Any row jumps into the full setup screen."""
        res = describe_resolution()
        options: list[dict] = []
        for role in ROLES:
            f = res["roles"][role]
            options.append({
                "title": f"{role}: {f['provider'][0]} / {f['model'][0]}",
                "value": f"role:{role}", "category": "roles",
                "description": f"thinking {'on' if f['thinking'][0] else 'off'}  "
                               f"(src {f['provider'][1]}/{f['model'][1]})",
                "on_select": (lambda v: self.action_open_setup()),
            })
        for pid in known_providers():
            present = res["providers"][pid]["key_present"]
            options.append({
                "title": f"key[{pid}]: {'present' if present else 'absent'}",
                "value": f"key:{pid}", "category": "keys",
                "on_select": (lambda v: self.action_open_setup()),
            })
        options.append({
            "title": "Open full setup (ctrl+s)...", "value": "__setup__", "category": "actions",
            "on_select": (lambda v: self.action_open_setup()),
        })
        self.push_screen(SelectDialog(title="Config (resolved: env > config > default)", options=options))

    def _cmd_doctor(self) -> None:
        """Run the provider/model preflight off the UI thread, then show a dialog."""
        self._write_activity("(doctor running…)", dim=True)
        self.run_worker(
            self._run_doctor_report,
            thread=True,
            name="doctor",
            group="doctor",
            exclusive=True,
            exit_on_error=False,
        )

    def _show_doctor_dialog(self, rows: list[dict] | None) -> None:
        rows = rows or []
        options = [
            {"title": f"{r.get('role')}  {r.get('provider')}/{r.get('model')}: {r.get('status', '?')}",
             "value": r.get("model", "?"), "category": "preflight",
             "description": r.get("note", "")}
            for r in rows
        ] or [{"title": "(no checks run)", "value": "__none__"}]
        self.push_screen(SelectDialog(title="Doctor: provider/model preflight", options=options))

    def _run_doctor_report(self) -> list[dict]:
        """Preflight rows, via the injected seam or the shared CLI doctor logic."""
        if self._doctor_fn is not None:
            return self._doctor_fn()
        try:
            from relay import doctor

            checks = doctor._doctor_checks(self._models, None)
            clients = doctor._build_provider_clients(checks)
            rows, _ = doctor._run_doctor(checks, clients)
            return rows
        except Exception as exc:  # noqa: BLE001 -- never crash the TUI on a preflight
            note = friendly_provider_error(str(exc).splitlines()[0][:120])
            return [{"role": "?", "provider": "?", "model": "?",
                     "status": "FAILED", "note": note}]

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        """Deliver off-thread /model list + /doctor results onto the UI thread."""
        worker = event.worker
        if event.state is not WorkerState.SUCCESS:
            if event.state is WorkerState.ERROR and worker.name in ("doctor", "list-models"):
                err = worker.error
                note = friendly_provider_error(
                    str(err).splitlines()[0][:120] if err else "worker failed"
                )
                self._write_activity(f"({worker.name} failed: {note})", dim=True)
            return
        if worker.name == "doctor":
            self._show_doctor_dialog(worker.result)
        elif worker.name == "list-models":
            self._show_model_pick_dialog(list(worker.result or []))

    def _cmd_runs(self) -> None:
        """List recent runs (reusing the runlog reader) read-only in a dialog."""
        records = self._read_runs()
        recent = list(reversed(records))[:20]
        options = []
        for rec in recent:
            roles = rec.roles if isinstance(rec.roles, dict) else {}
            models_text = ", ".join(f"{k}:{v}" for k, v in roles.items()) or "-"
            totals = rec.totals if isinstance(rec.totals, dict) else {}
            cost = totals.get("cost_usd")
            cost_text = "-" if cost is None else f"${cost:.4f}"
            options.append({
                "title": f"{str(rec.run_id)[:8]}  {rec.status}",
                "value": rec.run_id, "category": "runs",
                "description": f"{models_text}  cost {cost_text}",
            })
        if not options:
            options = [{"title": "(no runs recorded yet)", "value": "__none__"}]
        self.push_screen(SelectDialog(title="Recent runs", options=options))

    def _read_runs(self) -> list:
        if self._runs_fn is not None:
            return self._runs_fn()
        try:
            from relay.runlog import default_log_path, load_records

            return load_records(default_log_path(self._root))
        except Exception:  # noqa: BLE001 -- a missing/odd log is just "no runs"
            return []

    def _cmd_assume(self, arg: str = "") -> None:
        """Pick the assumption level for this session (dialog or ``/assume <level>``).

        Each level carries a short description DERIVED from the real dial semantics
        (:func:`relay.config.assumption_summary`), so the text can't drift from what
        the brain is actually instructed to do. The current level is marked.
        """
        level = (arg or "").strip()
        if level in ASSUMPTION_LEVELS:
            self._set_assume(level)
            return
        options = []
        for lvl in ASSUMPTION_LEVELS:
            current = lvl == self._assumption_level
            options.append({
                "title": f"{lvl}  (current)" if current else lvl,
                "value": lvl, "category": "assumption",
                "description": assumption_summary(lvl),
                "on_select": (lambda v: self._set_assume(v)),
            })
        self.push_screen(SelectDialog(title="Assumption level (1 = assume freely .. 5 = ask)", options=options))

    def _set_assume(self, level: str) -> None:
        self._assumption_level = level
        self._update_status()

    def _cmd_profile(self, arg: str = "") -> None:
        """B1: pick a named assumption profile for this session (session-only)."""
        from relay.profiles import PROFILES, get_profile

        name = (arg or "").strip().lower()
        if name:
            if get_profile(name) is None:
                self._write_activity(f"/profile: unknown profile '{name}'")
                return
            self._set_profile(name)
            return
        options = []
        for pname in PROFILES:
            p = get_profile(pname)
            assert p is not None
            options.append({
                "title": pname,
                "value": pname,
                "category": "profile",
                "description": f"{p.description} (dial={p.assumption_level})",
                "on_select": (lambda v, n=pname: self._set_profile(n)),
            })
        self.push_screen(SelectDialog(title="Assumption profile", options=options))

    def _set_profile(self, name: str) -> None:
        from relay.profiles import get_profile

        p = get_profile(name)
        if p is None:
            return
        self._assumption_level = p.assumption_level
        self._write_activity(
            f"[profile] {p.name} · dial={p.assumption_level} · {p.description}"
        )
        self._update_status()

    def _cmd_cwd(self, arg: str = "") -> None:
        """Show the current session working dir and let the user set a new one.

        The working dir is session-sticky: a set here persists across subsequent
        goals (until changed) -- the next goal operates from it, not the launch
        root. Guarded to non-running states (the command's ``enabled`` predicate)."""
        path = (arg or "").strip()
        if path:
            ok, note = self._set_working_dir(path)
            if not ok:
                self._write_activity(f"/cwd: {note}")
            return
        current = self._session.working_dir
        self.push_screen(TextEntryDialog(
            title="Working directory (persists across goals)",
            label=f"Currently: {current}\nEnter a new directory "
                  "(relative to the current one, or absolute):",
            password=False, placeholder="e.g. lunar_lander_testing",
            on_submit=self._set_working_dir,
        ))

    def _set_working_dir(self, path: str) -> tuple[bool, str]:
        """Establish a new session working dir (must be an existing directory).

        Returns ``(ok, note)`` so the entry dialog can show a rejection inline. A
        relative path is resolved against the current working dir."""
        raw = (path or "").strip()
        if not raw:
            return False, "enter a directory"
        target = Path(raw)
        if not target.is_absolute():
            target = self._session.working_dir / target
        target = target.resolve()
        if not target.is_dir():
            return False, f"not an existing directory: {target}"
        self._session.set_working_dir(target)
        self._announce_working_dir(established=True)
        self._update_status()
        return True, f"working dir set to {target}"

    def _cmd_cost(self) -> None:
        """Show envelope + session spend. Dialog-driven; ZERO model calls.

        Session-only edits (ceilings / warn thresholds) mutate the live
        :class:`CostEnvelope` on the runner when a run is in flight — they do
        not write config/env.
        """
        session = self._session_total()
        runner = self._runner
        env = getattr(runner, "envelope", None) if runner is not None else None
        ledger = runner.ledger if runner is not None else None
        spent = ledger.total_cost() if ledger is not None else self._goal_cost
        remaining = env.remaining_cost(ledger) if env is not None else None
        title = "Envelope" if env is not None and (
            env.max_cost is not None or env.max_steps is not None
        ) else "Cost (Relay shows spend; you decide when to stop)"
        options = [
            {"title": f"Session total: ${session:.4f}", "value": "__session__", "category": "spend",
             "description": "Cumulative across all goals since launch or last reset"},
            {"title": f"This goal: ${(spent or 0):.4f}", "value": "__goal__", "category": "spend",
             "description": "Current (or last) goal spend"},
        ]
        if env is not None:
            cost_line = (
                f"Ceiling: ${env.max_cost:.4f} (scope={env.scope})"
                if env.max_cost is not None else "Ceiling: unbounded"
            )
            rem_line = (
                f"Remaining: ${remaining:.4f}" if remaining is not None else "Remaining: n/a"
            )
            options.append({
                "title": cost_line, "value": "__ceiling__", "category": "envelope",
                "description": rem_line + " · session-only raise via set-ceiling",
            })
            options.append({
                "title": f"Warn @ {', '.join(f'{t:.0%}' for t in env.warn_thresholds)}",
                "value": "__warn__", "category": "envelope",
                "description": "Soft thresholds (session-only; next boundary check)",
            })
            if env.max_cost is not None:
                options.append({
                    "title": "Raise cost ceiling +50% (session)", "value": "__raise__",
                    "category": "actions",
                    "description": "Mutates this run only — does not write config",
                    "on_select": (lambda v: self._session_raise_cost_ceiling()),
                })
        options.extend([
            {"title": f"Live counter: {'on' if self._cost_visible else 'off'}", "value": "__toggle__",
             "category": "actions", "description": "Show/hide the status-line per-goal counter",
             "on_select": (lambda v: self._toggle_cost_counter())},
            {"title": "Reset session total", "value": "__reset__", "category": "actions",
             "description": "Zero the session figure (a deliberate break; leaves the goal "
                            "counter and any run untouched)",
             "on_select": (lambda v: self._reset_session_cost())},
        ])
        self.push_screen(SelectDialog(title=title, options=options))

    def _session_raise_cost_ceiling(self) -> None:
        """Session-only: bump the in-flight envelope's max_cost by 50%."""
        runner = self._runner
        env = getattr(runner, "envelope", None) if runner is not None else None
        if env is None or env.max_cost is None:
            return
        env.max_cost = float(env.max_cost) * 1.5
        self._write_activity(
            f"[envelope] session ceiling raised to ${env.max_cost:.4f} (not saved to config)"
        )
        self._update_status()

    def _cmd_why(self) -> None:
        """A2: show the harness flight recorder for the current/last run (zero tokens)."""
        from relay.explain import HarnessReport, explain_events
        from relay.debug import redact_secrets

        runner = self._runner
        outcome = getattr(runner, "outcome", None) if runner is not None else None
        result = getattr(outcome, "result", None) if outcome is not None else None
        harness = getattr(result, "harness", None) if result is not None else None
        if harness is None and result is not None and getattr(result, "events", None):
            harness = explain_events(
                result.events,
                goal=getattr(result, "goal", ""),
                status=getattr(result, "status", ""),
                assumption_level=getattr(self, "_assumption_level", None),
                max_total_steps=getattr(result, "max_total_steps", None),
                max_cost=getattr(result, "max_cost", None),
                envelope=getattr(result, "envelope", None),
            ).to_dict()
        if not harness:
            self._write_activity("[why] no harness data yet — run a goal first")
            return
        fields = HarnessReport.__dataclass_fields__
        kwargs = {k: harness[k] for k in fields if k in harness}
        text = redact_secrets(HarnessReport(**kwargs).to_text())
        for line in text.splitlines():
            self._write_activity(line)

    def _cmd_route(self) -> None:
        """E3: spend-broker cockpit — active route, pins, freeze state."""
        from relay.router import ModelRouter, format_broker_line

        root = self._session.working_dir
        router = getattr(self, "_model_router", None)
        if router is None:
            router = ModelRouter.from_resolve(None, root=root)
            self._model_router = router
        envelope = None
        ledger = None
        runner = self._runner
        outcome = getattr(runner, "outcome", None) if runner is not None else None
        result = getattr(outcome, "result", None) if outcome is not None else None
        if result is not None:
            envelope = getattr(result, "envelope", None)
            ledger = getattr(result, "ledger", None)
        line = format_broker_line(router, envelope, ledger)
        self._write_activity(f"[route] {line}")
        c = router.contract
        if c is not None:
            self._write_activity(
                f"[route] brain={c.brain} hands={c.hands} "
                f"provider={c.provider_sort} freeze@{int(c.bump_freeze_fraction * 100)}% "
                f"frozen={router.bumps_frozen} phase={router.phase}"
            )
            self._write_activity(
                f"[route] /model is an explicit override (beats the router). "
                f"Pins: {c.pins or '{}'}"
            )

    def _cmd_memory(self) -> None:
        """A3: list durable shared memory; offer pin/forget for the first entries."""
        from relay.durable_memory import list_entries, pin_entry, forget_entry

        root = self._session.working_dir
        entries = list_entries(root)
        options = [
            {
                "title": f"{e.id}: {e.summary}" + (" [pinned]" if "pinned" in e.tags else ""),
                "value": e.id,
                "category": "entries",
                "description": e.detail[:120],
            }
            for e in entries[:20]
        ]
        if not options:
            options = [{"title": "(empty)", "value": "__empty__", "category": "entries",
                        "description": "No durable shared findings yet"}]
        else:
            first = entries[0]
            options.append({
                "title": f"Pin {first.id}", "value": "__pin__", "category": "actions",
                "description": "Keep this entry across budget trim",
                "on_select": (lambda v, eid=first.id: pin_entry(root, eid) and self._write_activity(f"[memory] pinned {eid}")),
            })
            options.append({
                "title": f"Forget {first.id}", "value": "__forget__", "category": "actions",
                "description": "Remove from durable shared store",
                "on_select": (lambda v, eid=first.id: forget_entry(root, eid) and self._write_activity(f"[memory] forgot {eid}")),
            })
        self.push_screen(SelectDialog(title="Durable shared memory", options=options))

    def _toggle_cost_counter(self) -> None:
        """Show/hide the status-line per-goal counter (the /cost toggle)."""
        self._cost_visible = not self._cost_visible
        self._update_status()

    def _reset_session_cost(self) -> None:
        """Zero the session cumulative -- a deliberate manual break. Does NOT touch the
        per-goal counter or any in-flight run."""
        self._session_cost = 0.0
        self._update_status()

    def _cmd_clear(self) -> None:
        """The DISTINCT full-session reset (like OpenCode's session_new): wipe the
        conversation, memory, plan, queue, and recall history and start fresh. This is
        deliberately DIFFERENT from STOP (esc), which abandons only the current PLAN and
        preserves the session. Guarded: never while a run is in flight (also gated by the
        command's ``enabled`` predicate)."""
        if _run_active(self):
            return
        # Reset the durable session state (transcript, memory, plan, queue, history,
        # goal, revisions). The working DIR is intentionally kept -- it is a workspace
        # location, not conversation. The cost counters zero too: a fresh session.
        self._session.reset()
        self._seen_turn_ids.clear()
        self._router.finish_run()  # ensure a clean IDLE (never leave an interrupt state)
        self._goal_cost = 0.0
        self._session_cost = 0.0
        self._conversation_lines = []
        self._activity_lines = []
        self._stream_rendered = []
        self._stream_plain = []
        # Fresh session: drop allowlist / pending steer / fold UI state too.
        self._session_approvals.clear()
        self._pending_steer = None
        self._tool_folds.clear()
        # Wipe the single stream (history rows + the live plan block) and its plan state.
        self._reset_plan()
        stream = self._stream()
        if stream is not None:
            try:
                stream.remove_children()
            except Exception:  # noqa: BLE001 -- stream not mounted (welcome view)
                pass
        self._update_status()

    # -- /log: a shareable, redacted debug export -------------------------------
    #
    # A beta tester who hits a problem runs /log and gets one timestamped Markdown
    # file capturing the whole picture -- config, outcome, conversation, activity,
    # plan, memory -- to attach to a GitHub issue. It is safe to paste in public BY
    # CONSTRUCTION: the builder writes key PRESENCE only (never a value), and the
    # whole bundle is run through redact_secrets (with the live key strings) as the
    # final step. Assembled from existing state -- ZERO model calls, no upload.

    def _cmd_log(self) -> None:
        """Open the scope dialog (current project / full session); the choice writes
        a timestamped, REDACTED debug .md to cwd and names the path."""
        options = [
            {"title": "Current project", "value": "current", "category": "scope",
             "description": "The most recent project's transcript, activity, and outcome",
             "on_select": (lambda v: self._write_debug_log("current"))},
            {"title": "Full session", "value": "session", "category": "scope",
             "description": "Everything this session (incl. the current project) -- for "
                            "repetitive issues across projects",
             "on_select": (lambda v: self._write_debug_log("session"))},
        ]
        self.push_screen(SelectDialog(
            title="Export a debug log -- which scope?", options=options))

    def _write_debug_log(self, scope: str) -> None:
        """Build the bundle for ``scope``, redact it, and write a timestamped .md to
        cwd; then confirm the full path. A write/permissions failure surfaces a
        friendly line (the friendly-error spirit), never a traceback."""
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        try:
            bundle = self._build_debug_bundle(scope, timestamp=timestamp)
        except Exception as exc:  # noqa: BLE001 -- never crash the TUI on export
            self._write_conversation(
                f"(could not assemble the debug log: {exc.__class__.__name__})"
            )
            return
        path = Path.cwd() / f"relay-debug-{timestamp}.md"
        try:
            path.write_text(bundle, encoding="utf-8")
        except OSError as exc:
            reason = exc.strerror or str(exc).splitlines()[0]
            self._write_conversation(f"(could not write the debug log: {reason})")
            return
        self._write_conversation(
            f"Debug log written to {path} -- safe to attach to a GitHub issue "
            "(no keys included)."
        )

    def _build_debug_bundle(self, scope: str, *, timestamp: str) -> str:
        """Assemble the redacted bundle for ``scope`` from EXISTING state (no model
        call). Current scope renders the current run's structured transcript; full
        session renders the session-spanning conversation buffer -- the structured
        outcome/plan/memory are the current project's in both (the app keeps no
        per-project history; the bundle header says so)."""
        runner = self._runner
        if scope == "session":
            transcript_lines = list(self._conversation_lines)
        else:
            if runner is None:
                transcript_lines = []
            else:
                transcript = runner.transcript
                if hasattr(transcript, "snapshot_turns"):
                    turns = transcript.snapshot_turns()
                else:
                    turns = list(getattr(transcript, "turns", []) or [])
                transcript_lines = [format_turn(t) for t in turns]
        activity_lines = list(self._activity_lines)

        outcome = runner.outcome if runner is not None else None
        cost = runner.ledger.total_cost() if runner is not None else None
        run = summarize_run(outcome, cost=cost)
        result = getattr(outcome, "result", None) if outcome is not None else None
        plan = getattr(result, "plan", None)
        memory = getattr(result, "memory", None)

        from relay import __version__

        return build_debug_bundle(
            scope=scope,
            version=__version__,
            python_version=platform.python_version(),
            platform_str=platform.platform(),
            resolution=describe_resolution(),
            assumption_level=self._assumption_level,
            max_total_steps=resolve_max_total_steps(),
            run=run,
            transcript_lines=transcript_lines,
            activity_lines=activity_lines,
            plan=plan,
            memory=memory,
            known_secrets=self._live_key_values(),
            timestamp=timestamp,
        )

    def _live_key_values(self) -> list[str]:
        """The actual resolved key strings (env or auth.json) per provider, handed to
        the redactor to strip VERBATIM. These are never written into the bundle --
        the builder emits key presence only; this list is the exact-removal backstop."""
        values: list[str] = []
        for pid in known_providers():
            try:
                profile = resolve_provider(pid)
                key = resolve_key(pid, profile.key_env)
            except Exception:  # noqa: BLE001 -- a bad provider id: skip it
                key = None
            if key:
                values.append(key)
        return values

    # -- cancel + clean shutdown (the money-leak guard) --------------------------

    def _dismiss_approve_dialog(self) -> None:
        """Drop a stale ApproveDialog if it is still the top screen (esc interrupt)."""
        try:
            if isinstance(self.screen, ApproveDialog):
                self.pop_screen()
        except Exception:  # noqa: BLE001 -- no screen / already dismissed
            pass

    def action_cancel_run(self) -> None:
        """esc = INTERRUPT, not teardown. A running run halts at the clean boundary and
        lands at the interrupt prompt (session intact); a SECOND esc (already
        interrupted) is STOP -- abandon the plan, keep the session."""
        if self._router.state is InputState.INTERRUPTED:
            self._dismiss_approve_dialog()
            self._stop_from_interrupt()
            return
        runner = self._runner
        if runner is not None and runner.is_running:
            runner.cancel()
            # Instant, visible acknowledgment -- never a silent cancel. The cancel flag
            # is set now; the engine halts at the next executor-CALL boundary (after the
            # in-flight call returns), so a long multi-call step stops within ~one call's
            # latency instead of running to the end of the step. The in-flight request is
            # never torn down (the money-leak guard); the worker still joins cleanly.
            # _handle_finished sees _interrupting and routes to the interrupt prompt.
            self._interrupting = True
            self._stopping = True
            self._dismiss_approve_dialog()
            self._write_activity("[interrupt] halting at the next boundary... (esc again to stop)")
            self._update_status()

    def _stop_from_interrupt(self) -> None:
        """STOP: abandon the interrupted plan but PRESERVE the session (conversation,
        cwd, memory, cost all stay). The user's next input begins fresh planning
        within the SAME session -- never a teardown (that is /clear)."""
        self._router.finish_run()  # back to a clean IDLE; session state untouched
        self._session.last_plan = None
        self._stopping = False
        self._interrupting = False
        self._stop_spin()  # no motion at the idle prompt (the plan was already settled)
        self._write_activity("[stopped] plan abandoned; session preserved (cwd/memory/cost kept)")
        self._update_status()

    async def action_quit(self) -> None:
        """Quit WITHOUT orphaning the worker: cancel, join (bounded), then exit."""
        self._quitting = True
        self._stop_anim()
        self._stop_spin()
        led = self._led_timer
        if led is not None:
            try:
                led.stop()
            except Exception:  # noqa: BLE001 -- already stopped/torn down
                pass
            self._led_timer = None
        runner = self._runner
        if runner is not None and runner.is_running:
            runner.cancel()
            # Join off the UI loop so in-flight call_from_thread marshals can
            # still drain (joining on-loop could deadlock until their timeout).
            await asyncio.get_running_loop().run_in_executor(
                None, runner.join, _JOIN_TIMEOUT_S
            )
        self.exit()
