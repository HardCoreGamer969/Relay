"""The Relay TUI: a welcome screen + a two-pane chat over the v0.0.11 bridge.

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
  as identity, and a dim hint. The working panes are NOT shown here.
- **Working** (after the first goal): the Conversation pane (the transcript
  thread -- the star) over the Activity pane (the noisy event firehose, kept
  OUT of the conversation), a status/model line, and the input box. The first
  submit hands off from welcome to working (see :mod:`relay.tui` animations).

The conversation render path is UNICODE-CLEAN: turn text is never ASCII-
sanitized here (the recurring cp1252 hazard belongs to the legacy console, not
Textual). The welcome art uses unicode block glyphs freely.

:func:`present_prompt` is the ONE chokepoint every user-facing question/prompt
string passes through before display. Today it is a pass-through; prompt 2's
experience-level projection slots in there without a refactor.
"""

from __future__ import annotations

import asyncio
import random

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Container, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, RichLog, Select, Static

from relay.bridge import (
    ACTION_ANSWER,
    ACTION_START,
    EVENT_PHASE,
    REQUEST_APPROVAL,
    REQUEST_REACTION,
    STATUS_ERROR,
    EngineRunner,
    InputRouter,
    InputState,
    RunOutcome,
    UiRequest,
)
from relay.config import ROLES, ModelConfig, describe_resolution, default_config, load_models
from relay.orchestrator import Event
from relay.providers import (
    DISCOVERY_LIST,
    known_providers,
    list_models as provider_list_models,
    resolve_provider,
    validate_model as provider_validate_model,
)
from relay.secrets import resolve_key, set_key as secrets_set_key
from relay.store import CONFIG_VERSION, load_config, save_config
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

# The rotating welcome greetings -- one shown per launch. Warmer than "Goal:";
# productive, inviting, a little character. Edit freely.
GREETINGS = (
    "What are we building today?",
    "What should we work on?",
    "Point me at something.",
    "What's the mission?",
    "Give me a goal.",
    "What are we shipping?",
    "Where do we start?",
)

# The rotating IDLE input placeholders -- one chosen per launch. Same warm voice
# as GREETINGS, but kept DISJOINT from it so the box never echoes the greeting
# shown right above it (a guarantee, not a coincidence -- see the test).
INPUT_PLACEHOLDERS = (
    "Describe the goal...",
    "What are we making?",
    "What needs doing?",
    "Name the task...",
    "What's the goal?",
)

# State-aware placeholders: the one box's PURPOSE changes with what the engine is
# waiting for, so the prompt should say what a submit now means. Short.
_STATE_PLACEHOLDERS = {
    InputState.AWAITING_REACTION: "React to the plan, or type 'ok'...",
    InputState.AWAITING_DECISION: "Your answer...",
    InputState.AWAITING_APPROVAL: "Approve this command? (y/n)...",
    InputState.PLANNING: "The agent is working... (esc to cancel)",
    InputState.EXECUTING: "The agent is working... (esc to cancel)",
}

# The RELAY wordmark hero: hand-built 5-row block glyphs, letterspaced wide. We
# can't reproduce the curved interlocking-R logo glyph in text, so the confident
# letterspaced wordmark IS the hero (legible beats a janky knockoff). Each glyph
# is a fixed 5x5 cell grid, so the assembled banner is a clean rectangle (which
# the glitch animator wants -- see below).
_WORDMARK_GLYPHS = {
    "R": ["████ ", "█   █", "████ ", "█  █ ", "█   █"],
    "E": ["█████", "█    ", "████ ", "█    ", "█████"],
    "L": ["█    ", "█    ", "█    ", "█    ", "█████"],
    "A": [" ███ ", "█   █", "█████", "█   █", "█   █"],
    "Y": ["█   █", " █ █ ", "  █  ", "  █  ", "  █  "],
}
_WORDMARK_GAP = "   "


def _build_wordmark(word: str = "RELAY", gap: str = _WORDMARK_GAP) -> str:
    """Assemble the letterspaced block wordmark as one multi-line string."""
    rows = [gap.join(_WORDMARK_GLYPHS[ch][r] for ch in word) for r in range(5)]
    return "\n".join(rows)


RELAY_WORDMARK = _build_wordmark()

# -- the glitch / datamosh animator -------------------------------------------

# Cyberpunk static: glyphs an unlocked cell flickers through before it resolves.
_GLITCH_GLYPHS = "▓▒░█▌▐╱╲╳<>/\\|=+*#%01"

# One routed animator, short by default -- the app is relaunched constantly
# during dev, so a long boot gets old fast. Modes: "short" (fully implemented),
# "off" (instant, no timers), "long" (stubbed to short for now).
_ANIM_FPS = 24
_STARTUP_SHORT_S = 0.45      # boot decode that resolves into the wordmark
_TRANSITION_SHORT_S = 0.4    # welcome -> working datamosh (short ALWAYS)


def _normalize_block(target: str) -> list[str]:
    """Split into equal-width rows so the glitch matrix is a clean rectangle."""
    lines = target.split("\n")
    width = max((len(line) for line in lines), default=0)
    return [line.ljust(width) for line in lines]


def _glitch_thresholds(lines: list[str]) -> list[list[float]]:
    """A stable per-cell lock-threshold matrix (computed once per animation).

    Each cell locks to its true value when progress crosses its threshold;
    stable across frames so a locked cell never flickers back to noise.
    """
    rng = random.Random()
    return [[rng.random() for _ in line] for line in lines]


def glitch_frame(
    lines: list[str],
    thresholds: list[list[float]],
    progress: float,
    shimmer: random.Random,
    *,
    direction: str = "in",
) -> str:
    """One datamosh frame: locked cells show the true glyph, the rest flicker.

    ``direction="in"`` resolves noise -> target (boot); ``"out"`` dissolves
    target -> noise (the welcome handoff). ``shimmer`` is re-rolled each frame so
    unlocked cells crackle. At ``progress>=1`` an "in" frame is fully the target;
    at ``progress<=0`` an "out" frame is fully the target.
    """
    out = []
    for row, line in enumerate(lines):
        chars = []
        for col, ch in enumerate(line):
            threshold = thresholds[row][col]
            locked = progress >= threshold if direction == "in" else progress < threshold
            chars.append(ch if locked else shimmer.choice(_GLITCH_GLYPHS))
        out.append("".join(chars))
    return "\n".join(out)


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


def model_identity(models: ModelConfig) -> str:
    """The brain/hands pairing as IDENTITY (welcome screen), not a status note.

    This is the user knowing which pairing they're about to spend money on,
    front and center -- so it reads as the machine's name, cleanly styled.
    """
    return f"brain ~{models.brain}  ·  hands ~{models.hands}"


def pick_greeting() -> str:
    """One greeting for this launch (rotation is by random choice)."""
    return random.choice(GREETINGS)


def pick_placeholder() -> str:
    """One idle input placeholder for this launch (rotation by random choice)."""
    return random.choice(INPUT_PLACEHOLDERS)


def placeholder_for_state(state: InputState, idle_placeholder: str) -> str:
    """Resolve the input placeholder for the current router state (pure, testable).

    The awaiting/busy states get their fixed cue from :data:`_STATE_PLACEHOLDERS`;
    idle (and the welcome screen) shows ``idle_placeholder`` -- the rotating phrase
    chosen for this launch.
    """
    return _STATE_PLACEHOLDERS.get(state, idle_placeholder)


# -- the brain<->hands activity feed (rendered from ALREADY-EMITTED events) ----
#
# Attribution so the back-and-forth reads as a dialogue: the brain (planner) and
# the hands (executor) are the two voices; "you" is the human; system lines carry
# no tag. This is PURE PRESENTATION of data the engine already put on the event
# stream -- it must never trigger a model call (see the zero-new-tokens guard test).
ACTOR_BRAIN = "brain"
ACTOR_HANDS = "hands"
ACTOR_YOU = "you"

_ACTOR_STYLES = {ACTOR_BRAIN: "magenta", ACTOR_HANDS: "green", ACTOR_YOU: "bold cyan"}


def describe_event_for_activity(event: Event) -> tuple[str | None, str]:
    """Map one engine event to ``(actor, line)`` for the activity feed.

    ``actor`` is ``brain`` / ``hands`` / ``you`` (or ``None`` for a system line).
    Every field read here is already present on the emitted event -- nothing is
    fetched, narrated, or summarized by a model.
    """
    kind = event.kind
    p = event.payload or {}
    msg = event.message

    if kind == "step_start":
        return ACTOR_BRAIN, f"-> step {p.get('index')}: {p.get('instruction', msg)}"
    if kind == "exec_action":
        return ACTOR_HANDS, msg  # describe_action text; observation appended by caller
    if kind == "exec_parse_failure":
        return ACTOR_HANDS, f"! parse failure: {p.get('snippet', '')}"
    if kind == "executor_question":
        return ACTOR_HANDS, f"? {p.get('question', msg)}"
    if kind == "brain_self_answered":
        return ACTOR_BRAIN, f"answers: {p.get('answer', '')}"
    if kind == "brain_escalated":
        return ACTOR_BRAIN, f"escalates: {p.get('question', msg)}"
    if kind == "user_decided":
        return ACTOR_YOU, f"decided: {p.get('answer', msg)}"
    if kind == "step_reviewed":
        return ACTOR_BRAIN, f"reviews step {p.get('index')}: {p.get('verdict', '')}"
    if kind == "step_done":
        return ACTOR_HANDS, f"done step {p.get('index')}: {p.get('outcome', '')}"
    if kind == "step_failed":
        return ACTOR_HANDS, f"failed step {p.get('index')}: {p.get('reason', '')}"
    if kind == "plan_created":
        return ACTOR_BRAIN, f"plan: {len(p.get('steps') or [])} step(s)"
    if kind == "plan_proposed":
        return ACTOR_BRAIN, f"proposed a plan ({len(p.get('steps') or [])} step(s))"
    if kind in ("plan_revised", "replanned"):
        return ACTOR_BRAIN, f"revised the plan ({len(p.get('steps') or [])} step(s))"
    if kind == "escalation":
        return ACTOR_BRAIN, msg
    if kind == "memory_write":
        return ACTOR_BRAIN, f"memory += [{p.get('kind', '')}] {p.get('summary', '')}"
    if kind == "scope_assessed":
        return ACTOR_BRAIN, f"scope: {p.get('scope', '')} -> {p.get('posture', '')}"
    if kind in ("scoping_question", "elicitation"):
        return ACTOR_BRAIN, f"asks: {p.get('question', msg)}"
    if kind == "user_reacted":
        return ACTOR_YOU, f"reacted: {p.get('reaction', msg)}"
    if kind == "committed":
        return ACTOR_YOU, "committed the plan"
    # status / transcript_compacted / not_committed / anything else: a system line.
    return None, msg


def setup_summary() -> str:
    """A plain, key-free summary of the current resolution (provider/model/key
    presence per role/provider). Reads :func:`describe_resolution` -- NEVER a key."""
    res = describe_resolution()
    lines = []
    for role in ROLES:
        f = res["roles"][role]
        thinking = "on" if f["thinking"][0] else "off"
        lines.append(
            f"{role}: {f['provider'][0]} / {f['model'][0]}  (thinking {thinking}; "
            f"src {f['provider'][1]}/{f['model'][1]})"
        )
    for pid in known_providers():
        present = res["providers"][pid]["key_present"]
        lines.append(f"key[{pid}]: {'present' if present else 'absent'}")
    return "\n".join(lines)


class SetupScreen(ModalScreen):
    """In-TUI provider setup: enter a key (masked), pick per-role models, toggle
    thinking -- for a beta user with no terminal/.env knowledge.

    All persistence goes through the Part-1 backend (auth.json 0o600 for keys,
    config.json for selections). Network-touching work (model listing, slug
    validation) is behind injectable seams so the screen is headless-testable and
    never hits the network in tests. Real unicode; consistent cyberpunk aesthetic.
    """

    BINDINGS = [("escape", "close", "Close setup")]

    CSS = """
    SetupScreen { align: center middle; }
    #setup-box {
        width: 80%; max-width: 100; height: auto; max-height: 90%;
        padding: 1 2; border: double $primary; background: $surface;
    }
    #setup-title { text-style: bold; content-align: center middle; }
    #setup-summary { color: $text-muted; margin: 1 0; }
    #setup-status { margin-top: 1; }
    .setup-section { margin-top: 1; text-style: bold; color: $secondary; }
    Select, Input, Checkbox { margin-bottom: 1; }
    """

    def __init__(
        self,
        *,
        models: ModelConfig,
        list_models_fn=None,
        validate_fn=None,
        on_saved=None,
    ) -> None:
        super().__init__()
        self._models = models
        # Seams (injected by tests; default to the real, network-touching funcs).
        self._list_models_fn = list_models_fn or provider_list_models
        self._validate_fn = validate_fn or provider_validate_model
        self._on_saved = on_saved
        self._provider_options = [(p, p) for p in known_providers()]
        # The last status message rendered (mirrored for headless tests).
        self.status_text = ""

    # -- layout ---------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="setup-box"):
            yield Static("Relay setup", id="setup-title")
            yield Static(setup_summary(), id="setup-summary")

            yield Label("Provider key", classes="setup-section")
            yield Select(self._provider_options, id="key-provider", allow_blank=False,
                         value=self._models.brain_provider)
            # password=True -> the field shows bullets; keys get screenshotted.
            yield Input(placeholder="paste the API key (hidden)", password=True, id="key-input")
            yield Button("Save key", id="save-key", variant="primary")

            for role in ROLES:
                provider = self._models.provider_for_role(role)
                yield Label(f"{role} model", classes="setup-section")
                yield Select(self._provider_options, id=f"{role}-provider",
                             allow_blank=False, value=provider)
                yield Input(value=self._models.for_role(role),
                            placeholder="model id / slug", id=f"{role}-model")
                # For a list provider, a selectable list of live ids (fills the
                # input on pick). For a manual provider it simply stays empty.
                yield Select(self._model_options(role, provider), id=f"{role}-model-list",
                             allow_blank=True)
                yield Checkbox("thinking", value=self._models.thinking_for_role(role),
                               id=f"{role}-thinking")
                yield Button(f"Save {role}", id=f"save-{role}")

            yield Static("", id="setup-status")
            yield Static("openrouter: type any slug  ·  deepseek: pick from the list  ·  esc to close",
                         id="setup-hint")

    # -- seams + helpers (testable) ------------------------------------------

    def _model_options(self, role: str, provider: str) -> list[tuple[str, str]]:
        """Selectable model-id options for a role's provider (``[]`` for manual)."""
        return [(mid, mid) for mid in self.models_for(provider)]

    def models_for(self, provider: str) -> list[str]:
        """Live model ids for a ``list`` provider (``[]`` for manual / on error)."""
        try:
            profile = resolve_provider(provider)
        except ValueError:
            return []
        if profile.discovery != DISCOVERY_LIST:
            return []
        try:
            return list(self._list_models_fn(provider))
        except Exception:  # noqa: BLE001 -- no key/network: just an empty list
            return []

    def save_key(self, provider: str, key: str) -> bool:
        """Store a key (masked-entered) to auth.json 0o600. Returns saved?."""
        key = (key or "").strip()
        if not key:
            self._set_status("[yellow]no key entered.[/yellow]")
            return False
        secrets_set_key(provider, key)  # the value is NEVER echoed back
        self._set_status(f"[green]stored a key for {provider}.[/green]")
        self._refresh_summary()
        self._notify_saved()
        return True

    def save_role(self, role: str, provider: str, model: str, thinking: bool) -> bool:
        """Validate (live) and persist a role's provider/model/thinking. Returns saved?."""
        model = (model or "").strip()
        if not model:
            self._set_status(f"[red]{role}: enter a model id.[/red]")
            return False
        ok, note = self._validate_fn(provider, model)
        if not ok:
            self._set_status(f"[red]{role} rejected:[/red] {note}")  # inline error, not saved
            return False
        config = load_config() or default_config()
        config.setdefault("version", CONFIG_VERSION)
        config.setdefault("roles", {})[role] = {
            "provider": provider, "model": model, "thinking": bool(thinking),
        }
        save_config(config)
        self._set_status(f"[green]saved {role}: {provider} / {model}.[/green]")
        self._refresh_summary()
        self._notify_saved()
        return True

    # -- widget event wiring --------------------------------------------------

    def on_select_changed(self, event: Select.Changed) -> None:
        sid = event.select.id or ""
        if sid.endswith("-provider") and not sid.startswith("key"):
            role = sid[: -len("-provider")]
            self._repopulate_model_list(role, str(event.value))
        elif sid.endswith("-model-list") and event.value not in (None, Select.BLANK):
            role = sid[: -len("-model-list")]
            self.query_one(f"#{role}-model", Input).value = str(event.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid == "save-key":
            provider = str(self.query_one("#key-provider", Select).value)
            self.save_key(provider, self.query_one("#key-input", Input).value)
            self.query_one("#key-input", Input).value = ""  # don't leave the key on screen
        elif bid.startswith("save-"):
            self._save_role_from_widgets(bid[len("save-"):])

    def _save_role_from_widgets(self, role: str) -> None:
        if role not in ROLES:
            return
        provider = str(self.query_one(f"#{role}-provider", Select).value)
        model = self.query_one(f"#{role}-model", Input).value
        thinking = self.query_one(f"#{role}-thinking", Checkbox).value
        self.save_role(role, provider, model, bool(thinking))

    def _repopulate_model_list(self, role: str, provider: str) -> None:
        try:
            select = self.query_one(f"#{role}-model-list", Select)
        except Exception:  # noqa: BLE001 -- not mounted yet
            return
        select.set_options(self._model_options(role, provider))

    def _refresh_summary(self) -> None:
        try:
            self.query_one("#setup-summary", Static).update(setup_summary())
        except Exception:  # noqa: BLE001 -- not mounted
            pass

    def _set_status(self, message: str) -> None:
        self.status_text = message
        try:
            self.query_one("#setup-status", Static).update(message)
        except Exception:  # noqa: BLE001 -- not mounted
            pass

    def _notify_saved(self) -> None:
        if self._on_saved is not None:
            self._on_saved()

    def action_close(self) -> None:
        self.dismiss()


class RelayTuiApp(App):
    """A welcome screen that hands off to a two-pane chat over the engine."""

    TITLE = "Relay"

    CSS = """
    Screen { layout: vertical; }

    /* -- the welcome state (shown first; hidden once work begins) -- */
    #welcome { height: 1fr; align: center middle; }
    #welcome-inner {
        width: auto;
        height: auto;
        align: center middle;
        padding: 1 4;
        border: double $primary;
    }
    #brand { width: auto; content-align: center middle; text-style: bold; }
    #greeting { width: auto; content-align: center middle; text-style: bold; margin-top: 1; }
    #indicator { width: auto; content-align: center middle; color: $text-muted; margin-top: 1; }
    #hint { width: auto; content-align: center middle; color: $text-muted; text-style: dim; margin-top: 1; }

    /* -- the working state (hidden until the first goal) -- */
    #working { height: 1fr; layout: vertical; display: none; }
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
    ) -> None:
        super().__init__()
        self._root = root
        self._models = models if models is not None else load_models()
        self._client = client
        # Setup-flow seams (injected by tests; default to the real provider funcs).
        self._list_models_fn = list_models_fn
        self._validate_fn = validate_fn
        self._assumption_level = assumption_level
        self._auto_approve = auto_approve
        self._run_kwargs = run_kwargs
        # TODO(prompt-2): drive anim_mode from persisted settings + a launch
        # counter (a longer "first few launches" variant for "long"). Hardcoded
        # "short" for now; "off" is a clean instant no-op.
        self._anim_mode = anim_mode
        self._anim_timer = None
        self._router = InputRouter()
        self._runner: EngineRunner | None = None
        self._quitting = False
        # "welcome" until the first goal hands off to "working" (one-way).
        self._view = "welcome"
        self._greeting = pick_greeting()
        self._placeholder = pick_placeholder()  # the idle prompt phrase for this launch
        self._indicator_text = model_identity(self._models)
        self._first_run = False  # set when the empty-state setup is offered on launch
        # The render-path buffers: exactly the strings handed to the widgets,
        # kept so headless tests can assert on the render path directly.
        self._conversation_lines: list[str] = []
        self._activity_lines: list[str] = []
        self._status_text = ""
        self._seen_turn_ids: set[str] = set()

    # -- layout ---------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Container(id="welcome"):
            with Vertical(id="welcome-inner"):
                yield Static(RELAY_WORDMARK, id="brand")
                yield Static(self._greeting, id="greeting")
                yield Static(self._indicator_text, id="indicator")
                yield Static("esc to cancel  ·  ctrl+q to quit", id="hint")
        with Container(id="working"):
            conversation = RichLog(id="conversation", wrap=True, markup=False, highlight=False)
            conversation.border_title = "Conversation"
            yield conversation
            activity = RichLog(id="activity", wrap=True, markup=False, highlight=False)
            activity.border_title = "Activity"
            yield activity
            yield Static(id="status")
        yield Input(id="prompt", placeholder=self._placeholder)

    def on_mount(self) -> None:
        # The model indicator is visible from launch, BEFORE the first message
        # (promoted on the welcome screen; mirrored into the status buffer too).
        self._update_status()
        self.query_one("#prompt", Input).focus()
        self.set_interval(_SYNC_INTERVAL_S, self._sync_transcript)
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
        """Empty-state: make the next step obvious, then open setup (escapable)."""
        self._first_run = True
        try:
            self.query_one("#hint", Static).update(
                "Add a provider key to get started -- ctrl+s, or set RELAY_* env vars"
            )
        except Exception:  # noqa: BLE001 -- hint not present
            pass
        self.action_open_setup()

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

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value
        event.input.value = ""
        outcome = self._router.submit(text)
        if outcome.action == ACTION_START:
            if self._view == "welcome":
                self._begin_first_run(text)
            else:
                self._start_run(text)
        elif outcome.action == ACTION_ANSWER:
            # Answers that become transcript turns render via the sync pass;
            # approval answers never reach the transcript, so echo them here.
            if outcome.kind == REQUEST_APPROVAL:
                self._write_conversation(f"you (approval): {text}")
        elif text.strip():
            self._write_activity("(input ignored: the engine is busy)")
        self._update_status()

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
        # A REACTION ask is the proposal: its full numbered plan is NOT dumped into
        # the conversation -- that pane keeps only the human story (the headline
        # turn + the surfaced assumptions, both rendered from the plan_proposed
        # event via _render_plan_split); the full steps live in Activity. Other
        # asks (decision/approval) still surface their prompt when it adds detail
        # beyond the last transcript turn (e.g. the approval command).
        if request.kind != REQUEST_REACTION:
            last_turn_text = self._last_synced_turn_text()
            if request.prompt.strip() != (last_turn_text or "").strip():
                for line in present_prompt(request.prompt).splitlines():
                    self._write_conversation(f"brain: {line}" if line.strip() else "")
        self._update_status()

    def _handle_event(self, event: Event) -> None:
        """One engine event: phase changes steer the router; the rest stream into
        the Activity pane as an attributed brain<->hands feed.

        Everything shown here is read from the event the engine ALREADY emitted --
        the render path makes no model call (proven by the zero-new-tokens guard).
        """
        if event.kind == EVENT_PHASE:
            # Internal routing only -- not surfaced as a feed line.
            self._router.set_phase(event.payload.get("phase", ""))
        else:
            actor, line = describe_event_for_activity(event)
            if line:
                self._write_activity(line, actor=actor)
            # The hands' raw output is already captured on exec_action -- show a
            # snippet so the gears are visible (no generation to obtain it).
            if event.kind == "exec_action":
                observation = " ".join((event.payload.get("observation") or "").split())
                if observation:
                    self._write_activity(f"    {observation[:200]}", dim=True)
            self._render_plan_split(event)
        self._sync_transcript()
        self._update_status()

    def _render_plan_split(self, event: Event) -> None:
        """Dual-fidelity split, rendered from data the engine ALREADY emitted:

        - numbered executor **steps** -> Activity (the "what's actually being
          built" detail);
        - surfaced **assumptions** (the ``<assume>`` items) -> Conversation (what a
          human reacts to and can judge).

        The headline is the transcript proposal turn (rendered by the sync pass).
        Nothing is regenerated or re-summarized -- both lists come straight off the
        plan_proposed / plan_revised event payload.
        """
        payload = event.payload or {}
        steps = payload.get("steps")
        if isinstance(steps, list) and steps:
            for i, step in enumerate(steps, 1):
                self._write_activity(f"    {i}. {step}", dim=True)
        assumptions = payload.get("assumptions")
        if isinstance(assumptions, list) and assumptions:
            for assumption in assumptions:
                self._write_conversation(f"brain (assumes): {assumption}")

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

    def _write_activity(self, line: str, *, actor: str | None = None, dim: bool = False) -> None:
        """Append one activity line. ``actor`` (brain/hands/you) prefixes a colored
        tag so the feed reads as a dialogue; ``dim`` styles detail lines.

        The buffer keeps a plain tagged string (what tests assert on); the widget
        gets a pre-styled ``Text`` -- built via ``Text.append`` so untrusted content
        (tool output, model text) is never parsed as console markup.
        """
        self._activity_lines.append(f"{actor} | {line}" if actor else line)
        text = Text()
        if actor:
            text.append(actor, style=_ACTOR_STYLES.get(actor, ""))
            text.append(" | ", style="dim")
        text.append(line, style="dim" if dim else "")
        self.query_one("#activity", RichLog).write(text)

    def _update_status(self) -> None:
        state = self._router.state
        hint = _STATE_HINTS.get(state, "")
        self._status_text = (
            f"[{state.value}] {hint}  |  "
            f"brain={self._models.brain}  hands={self._models.hands}"
        )
        self.query_one("#status", Static).update(self._status_text)
        # The input box's placeholder tracks what a submit now means (Fix 1).
        self.query_one("#prompt", Input).placeholder = placeholder_for_state(
            state, self._placeholder
        )

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
        not just env. (A run already in flight keeps its own resolved models.)
        """
        self._models = load_models()
        self._indicator_text = model_identity(self._models)
        try:
            self.query_one("#indicator", Static).update(self._indicator_text)
        except Exception:  # noqa: BLE001 -- indicator not present (e.g. mid-working)
            pass
        self._update_status()

    # -- cancel + clean shutdown (the money-leak guard) --------------------------

    def action_cancel_run(self) -> None:
        runner = self._runner
        if runner is not None and runner.is_running:
            runner.cancel()
            self._write_activity("[cancel] stop requested (takes effect at the next step boundary)")

    async def action_quit(self) -> None:
        """Quit WITHOUT orphaning the worker: cancel, join (bounded), then exit."""
        self._quitting = True
        self._stop_anim()
        runner = self._runner
        if runner is not None and runner.is_running:
            runner.cancel()
            # Join off the UI loop so in-flight call_from_thread marshals can
            # still drain (joining on-loop could deadlock until their timeout).
            await asyncio.get_running_loop().run_in_executor(
                None, runner.join, _JOIN_TIMEOUT_S
            )
        self.exit()
