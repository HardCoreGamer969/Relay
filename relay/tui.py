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
import re
from dataclasses import dataclass
from typing import Callable

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
from relay.config import (
    ASSUMPTION_LEVELS,
    ROLES,
    ModelConfig,
    assumption_summary,
    describe_resolution,
    default_config,
    env_override_for,
    load_models,
)
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

# The states where the engine is ACTIVELY generating (the model is genuinely
# running). The slash popover is suppressed only here -- every other state (idle and
# the awaiting-user states) accepts a slash command (see ``_slash_allowed``).
_GENERATING_STATES = (InputState.PLANNING, InputState.EXECUTING)

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

# How long the live cost counter stays highlighted after it climbs (a single
# transient style flip on change -- no animation loop, no thread, zero model calls).
_COST_PULSE_S = 0.5


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


# -- friendly provider errors (the catch-all so raw API JSON never reaches a user) --

# Pretty provider labels for user-facing error text (fall back to the raw id).
_PROVIDER_LABELS = {"openrouter": "OpenRouter", "deepseek": "DeepSeek"}

# Markers that betray a raw provider/API error blob (JSON / status line) we must
# never surface verbatim.
_RAW_ERROR_MARKERS = ("{'error'", '{"error"', "'raw'", '"raw"', "error code:", "traceback")


def _provider_label(provider: str | None) -> str:
    return _PROVIDER_LABELS.get(provider, provider) if provider else "The provider"


def _is_raw_provider_error(text: str) -> bool:
    """Whether ``text`` looks like a raw provider/API error blob (don't show it raw)."""
    low = text.lower()
    return any(marker in low for marker in _RAW_ERROR_MARKERS)


def _http_status(text: str) -> str | None:
    """Pull an HTTP-ish 4xx/5xx status code out of a provider error string."""
    match = re.search(r"\b([45]\d\d)\b", text)
    return match.group(1) if match else None


def friendly_provider_error(error, *, provider: str | None = None, model: str | None = None) -> str:
    """Render a raw provider/API error as a friendly, ASCII-safe one-liner.

    THE catch-all net: at every point a provider error would reach the UI (the
    run-error path and the slash live calls -- validation, listing, doctor), this
    states what failed, which provider/model, and a short hint to re-pick -- and
    NEVER includes the raw ``{'error': {... 'raw': ...}}`` payload (which may be
    logged at debug elsewhere, but not shown). Text that does NOT look like a raw
    provider error is returned unchanged, so a clean validation note ("'x' is not in
    deepseek's live model list") and a plain non-provider error read normally.
    """
    text = str(error or "").strip()
    if not _is_raw_provider_error(text):
        return text
    label = _provider_label(provider)
    code = _http_status(text)
    code_note = f" (HTTP {code})" if code else ""
    if model:
        lead = (
            f"{label} rejected the request -- '{model}' may not be a valid {label} model"
            if code == "400"
            else f"{label} returned an error{code_note} for '{model}'"
        )
        return f"{lead}. Use /model or /provider to pick a valid one."
    return (
        f"{label} returned an error{code_note}. The model or provider may be invalid -- "
        "check with /doctor, or re-pick via /model or /provider."
    )


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


def persist_role(
    role: str, provider: str, model: str, thinking: bool, *, validate_fn=None
) -> tuple[bool, str]:
    """Validate a (provider, model) live, then persist the role to config.json.

    The ONE place a role selection is written -- shared by the SetupScreen and the
    ``/model`` slash command so they can never fork (same validation, same write).
    ``validate_fn`` defaults to the shared :func:`relay.providers.validate_model`.
    Returns ``(saved?, note)``; does not persist on validation failure.
    """
    validate_fn = validate_fn or provider_validate_model
    model = (model or "").strip()
    if not model:
        return False, "enter a model id"
    ok, note = validate_fn(provider, model)
    if not ok:
        return False, note
    config = load_config() or default_config()
    config.setdefault("version", CONFIG_VERSION)
    config.setdefault("roles", {})[role] = {
        "provider": provider, "model": model, "thinking": bool(thinking),
    }
    save_config(config)
    return True, note


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
        """Validate (live) and persist a role's provider/model/thinking. Returns saved?.

        Delegates to the shared :func:`persist_role` (same path the ``/model`` slash
        command uses) so validation + persistence never fork.
        """
        ok, note = persist_role(role, provider, model, thinking, validate_fn=self._validate_fn)
        if not ok:
            note = friendly_provider_error(note, provider=provider, model=model)
            self._set_status(f"[red]{role} rejected:[/red] {note}")  # inline error, not saved
            return False
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


# ============================================================================
# Slash commands: a dialog-driven control plane (v0.0.17)
# ============================================================================
#
# Typing "/" in the prompt opens a filterable popover of commands; each command's
# run() opens a DIALOG or performs a clean no-arg action. NO command parses inline
# arguments, and NO command (especially /key) ever reads a value out of the prompt
# text. Slash commands are a thin front door that LAUNCHES the existing v0.0.16
# flows (masked key entry, live model listing, validation, persistence, doctor,
# runs) -- they reuse those functions, never fork them.


@dataclass(frozen=True)
class Command:
    """One slash command as a data record.

    ``name`` is the slash trigger (``"model"`` -> typed ``/model``); ``title`` /
    ``description`` are human text; ``category`` groups it in lists; ``run(app)``
    opens a dialog or performs the action (it takes only the app -- never a value
    parsed from the input); ``enabled(app)`` optionally hides the command in the
    current state (e.g. mid-run). Adding a command is adding a record to
    :data:`COMMANDS`.
    """

    name: str
    title: str
    description: str
    category: str
    run: Callable  # run(app) -> None
    enabled: Callable | None = None  # enabled(app) -> bool


def _run_active(app) -> bool:
    """Whether a run is in flight (used by ``enabled`` predicates)."""
    runner = getattr(app, "_runner", None)
    return runner is not None and getattr(runner, "is_running", False)


def visible_commands(app) -> list[Command]:
    """Commands available in the app's current state (``enabled`` honored)."""
    return [c for c in COMMANDS if c.enabled is None or c.enabled(app)]


def filter_commands(app, query: str) -> list[Command]:
    """Visible commands whose name/title matches ``query`` (substring; empty = all)."""
    q = (query or "").strip().lower()
    out = []
    for command in visible_commands(app):
        if not q or q in command.name.lower() or q in command.title.lower():
            out.append(command)
    return out


class PromptInput(Input):
    """The main prompt input. When the slash popover is open it routes up/down/esc
    to the popover (Enter is handled via ``Input.Submitted`` in the app)."""

    def on_key(self, event) -> None:
        app = self.app
        if not getattr(app, "_popover_open", False):
            return
        if event.key == "down":
            app._popover_move(1); event.prevent_default(); event.stop()
        elif event.key == "up":
            app._popover_move(-1); event.prevent_default(); event.stop()
        elif event.key == "escape":
            app._popover_close(); event.prevent_default(); event.stop()


class FilterInput(Input):
    """A dialog's filter field: up/down move the dialog highlight (the screen owns
    selection); typing filters via the screen's ``apply_filter``."""

    def on_key(self, event) -> None:
        screen = self.screen
        if event.key == "down" and hasattr(screen, "move"):
            screen.move(1); event.prevent_default(); event.stop()
        elif event.key == "up" and hasattr(screen, "move"):
            screen.move(-1); event.prevent_default(); event.stop()


_DIALOG_CSS = """
SelectDialog, TextEntryDialog, SegmentedControl { align: center middle; }
#dialog-box {
    width: 80%; max-width: 100; height: auto; max-height: 90%;
    padding: 1 2; border: double $primary; background: $surface;
}
#dialog-title { text-style: bold; content-align: center middle; }
#dialog-list { margin: 1 0; }
#segment-row { margin: 1 0; content-align: center middle; }
#dialog-hint, #entry-hint { color: $text-muted; text-style: dim; margin-top: 1; }
#dialog-filter, #entry-input { margin-bottom: 1; }
#entry-status { margin-top: 1; }
"""


class SelectDialog(ModalScreen):
    """One generic filterable selection dialog -- the primitive every list command
    (``/help``, ``/model``, ``/config``, ``/doctor``, ``/runs``, ``/assume``) opens.

    ``options`` is a list of dicts: ``{title, value, description?, category?,
    on_select?}``. Options are grouped by ``category`` when present; typing filters,
    arrows move, Enter calls the highlighted option's ``on_select(value)``.
    """

    BINDINGS = [("escape", "close", "Close")]
    CSS = _DIALOG_CSS

    def __init__(self, *, title: str, options: list[dict]) -> None:
        super().__init__()
        self._title = title
        self._options = list(options)
        self._visible: list[dict] = list(self._options)
        self._highlight = 0

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog-box"):
            yield Static(self._title, id="dialog-title")
            yield FilterInput(placeholder="type to filter...", id="dialog-filter")
            yield Static(id="dialog-list")
            yield Static("up/down move  ·  enter choose  ·  esc close", id="dialog-hint")

    def on_mount(self) -> None:
        self.apply_filter("")
        self.query_one("#dialog-filter", Input).focus()

    # -- testable core --------------------------------------------------------

    def apply_filter(self, text: str) -> None:
        q = (text or "").strip().lower()

        def match(option: dict) -> bool:
            hay = " ".join(
                str(option.get(k, "")) for k in ("title", "value", "description", "category")
            ).lower()
            return not q or q in hay

        self._visible = [o for o in self._options if match(o)]
        self._highlight = 0
        self._refresh_list()

    def visible_values(self) -> list:
        return [o.get("value") for o in self._visible]

    def move(self, delta: int) -> None:
        if not self._visible:
            return
        self._highlight = max(0, min(len(self._visible) - 1, self._highlight + delta))
        self._refresh_list()

    def select_highlighted(self) -> None:
        if self._visible:
            self.choose(self._visible[self._highlight].get("value"))

    def choose(self, value) -> None:
        """Dismiss and invoke the chosen option's ``on_select`` (if any)."""
        chosen = next((o for o in self._visible if o.get("value") == value), None)
        if chosen is None:
            return
        self.dismiss()
        callback = chosen.get("on_select")
        if callback is not None:
            callback(value)

    # -- rendering ------------------------------------------------------------

    def _refresh_list(self) -> None:
        # NOTE: do NOT name this ``_render`` -- that shadows Textual's
        # ``Widget._render`` (which must return a Visual) and renders the screen None.
        try:
            widget = self.query_one("#dialog-list", Static)
        except Exception:  # noqa: BLE001 -- not mounted (headless logic-only use)
            return
        widget.update(self._list_renderable())

    def _list_renderable(self) -> Text:
        text = Text()
        if not self._visible:
            text.append("(no matches)", style="dim")
            return text
        last_category = object()
        for i, option in enumerate(self._visible):
            category = option.get("category")
            if category and category != last_category:
                text.append(f"{category}\n", style="bold")
                last_category = category
            marker = "> " if i == self._highlight else "  "
            style = "reverse" if i == self._highlight else ""
            line = f"{marker}{option.get('title', option.get('value', ''))}"
            text.append(line, style=style)
            desc = option.get("description")
            if desc:
                text.append(f"  -  {desc}", style="dim")
            text.append("\n")
        return text

    # -- widget wiring --------------------------------------------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "dialog-filter":
            event.stop()
            self.apply_filter(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "dialog-filter":
            event.stop()
            self.select_highlighted()

    def action_close(self) -> None:
        self.dismiss()


class TextEntryDialog(ModalScreen):
    """A single-field entry dialog -- masked (``password=True``) for a key, plain
    for a manual model slug. ``on_submit(value) -> (ok, note)``; the dialog stays
    open (showing the note) on failure, dismisses on success. The value is read
    ONLY from this dialog's own field -- never from the chat prompt."""

    BINDINGS = [("escape", "close", "Close")]
    CSS = _DIALOG_CSS

    def __init__(
        self, *, title: str, label: str, on_submit, password: bool = False,
        placeholder: str = "",
    ) -> None:
        super().__init__()
        self._title = title
        self._label = label
        self._on_submit = on_submit
        self._password = password
        self._placeholder = placeholder
        self.status_text = ""

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog-box"):
            yield Static(self._title, id="dialog-title")
            yield Label(self._label)
            yield Input(password=self._password, placeholder=self._placeholder, id="entry-input")
            yield Button("Save", id="entry-save", variant="primary")
            yield Static("", id="entry-status")
            yield Static("enter to save  ·  esc to cancel", id="entry-hint")

    def on_mount(self) -> None:
        self.query_one("#entry-input", Input).focus()

    def submit(self) -> bool:
        """Read THIS dialog's field and hand it to ``on_submit``. Returns saved?."""
        value = self.query_one("#entry-input", Input).value
        ok, note = self._on_submit(value)
        if ok:
            self.dismiss()
            return True
        self._set_status(f"[red]{note}[/red]")
        return False

    def _set_status(self, message: str) -> None:
        self.status_text = message
        try:
            self.query_one("#entry-status", Static).update(message)
        except Exception:  # noqa: BLE001 -- not mounted
            pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "entry-save":
            event.stop()
            self.submit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "entry-input":
            event.stop()
            self.submit()

    def action_close(self) -> None:
        self.dismiss()


class SegmentRow(Static):
    """The focusable key-sink for a :class:`SegmentedControl` (no text field, so
    the row itself takes focus and routes left/right/enter/escape to the screen)."""

    can_focus = True

    def on_key(self, event) -> None:
        screen = self.screen
        if not hasattr(screen, "move"):
            return
        if event.key in ("left", "h"):
            screen.move(-1); event.prevent_default(); event.stop()
        elif event.key in ("right", "l"):
            screen.move(1); event.prevent_default(); event.stop()
        elif event.key == "enter":
            screen.select_highlighted(); event.prevent_default(); event.stop()
        elif event.key == "escape":
            screen.action_close(); event.prevent_default(); event.stop()


class SegmentedControl(ModalScreen):
    """A reusable horizontal choose-one toggle (the analog of :class:`SelectDialog`
    for a small fixed set picked with LEFT/RIGHT, with wrap-around).

    ``options`` is an ordered list of ``{label, value}``; LEFT/RIGHT move the
    highlight (wrapping at both ends), Enter commits the highlighted option (calls
    ``on_select(value)`` then dismisses), Esc cancels. It's a ModalScreen (same CSS
    family / aesthetic as the other dialogs), so it never touches the prompt input
    or the InputRouter. The testable core (``move`` / ``highlighted_value`` /
    ``select_highlighted``) is kept separate from rendering -- mirroring SelectDialog.
    """

    BINDINGS = [
        ("left", "move_left", "Prev"),
        ("right", "move_right", "Next"),
        ("escape", "close", "Cancel"),
    ]
    CSS = _DIALOG_CSS

    def __init__(
        self, *, title: str, options: list[dict], start_index: int = 0, on_select=None
    ) -> None:
        super().__init__()
        self._title = title
        self._options = list(options)
        n = len(self._options)
        self._index = (start_index % n) if n else 0
        self._on_select = on_select

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog-box"):
            yield Static(self._title, id="dialog-title")
            yield SegmentRow(id="segment-row")
            yield Static("left/right to choose  ·  enter to confirm  ·  esc to cancel",
                         id="dialog-hint")

    def on_mount(self) -> None:
        self._refresh_segments()
        self.query_one("#segment-row", SegmentRow).focus()

    # -- testable core (no rendering) ----------------------------------------

    def move(self, delta: int) -> None:
        """Move the highlight by ``delta`` with WRAP-AROUND at both ends."""
        n = len(self._options)
        if n == 0:
            return
        self._index = (self._index + delta) % n
        self._refresh_segments()

    def highlighted_value(self):
        """The currently highlighted option's value (``None`` if there are none)."""
        if not self._options:
            return None
        return self._options[self._index].get("value")

    def select_highlighted(self) -> None:
        """Commit the highlighted option: dismiss, then call ``on_select(value)``."""
        if not self._options:
            self.dismiss()
            return
        value = self._options[self._index].get("value")
        self.dismiss()
        if self._on_select is not None:
            self._on_select(value)

    # -- rendering ------------------------------------------------------------

    def _refresh_segments(self) -> None:
        try:
            self.query_one("#segment-row", SegmentRow).update(self._segments_text())
        except Exception:  # noqa: BLE001 -- not mounted (logic-only use in tests)
            pass

    def _segments_text(self) -> Text:
        text = Text()
        if not self._options:
            text.append("(no options)", style="dim")
            return text
        for i, option in enumerate(self._options):
            if i:
                text.append("  <  >  ", style="dim")  # the toggle's left/right hint
            label = str(option.get("label", option.get("value", "")))
            if i == self._index:
                text.append(f"[ {label} ]", style="reverse bold")
            else:
                text.append(f"  {label}  ")
        return text

    # -- key actions (real-terminal bindings; tests drive the core directly) --

    def action_move_left(self) -> None:
        self.move(-1)

    def action_move_right(self) -> None:
        self.move(1)

    def action_close(self) -> None:
        self.dismiss()


# The registry -- one list; adding a command is adding a record. run(app) opens a
# dialog or does a clean action. Categories group the list in /help and the popover.
COMMANDS: list[Command] = [
    Command("help", "Help", "List all commands", "general",
            run=lambda app: app._cmd_help()),
    Command("model", "Model", "Pick the model for a role", "config",
            run=lambda app: app._cmd_model()),
    Command("provider", "Provider", "Set a role's provider, then its model", "config",
            run=lambda app: app._cmd_provider()),
    Command("key", "Key", "Add a provider API key (masked)", "config",
            run=lambda app: app._cmd_key()),
    Command("config", "Config", "Show the resolved config", "config",
            run=lambda app: app._cmd_config()),
    Command("doctor", "Doctor", "Preflight each role's provider/model", "ops",
            run=lambda app: app._cmd_doctor()),
    Command("runs", "Runs", "List recent runs", "ops",
            run=lambda app: app._cmd_runs()),
    Command("assume", "Assume", "Set the assumption level for this session", "ops",
            run=lambda app: app._cmd_assume()),
    Command("clear", "Clear", "Clear the conversation + activity panes", "ops",
            run=lambda app: app._cmd_clear(), enabled=lambda app: not _run_active(app)),
]


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

    /* -- the slash-command popover (shown only while typing a /command) -- */
    #command-popover {
        display: none;
        height: auto;
        max-height: 12;
        margin: 0 1;
        padding: 0 1;
        border: round $primary;
        background: $surface;
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
    ) -> None:
        super().__init__()
        self._root = root
        self._models = models if models is not None else load_models()
        self._client = client
        # Setup-flow seams (injected by tests; default to the real provider funcs).
        self._list_models_fn = list_models_fn
        self._validate_fn = validate_fn
        # Slash-command seams (injected by tests; default to the real CLI logic).
        self._doctor_fn = doctor_fn
        self._runs_fn = runs_fn
        # The slash-command popover state (mirrored for headless tests).
        self._popover_open = False
        self._popover_commands: list[Command] = []
        self._popover_index = 0
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
        yield Static(id="command-popover")
        yield PromptInput(id="prompt", placeholder=self._placeholder)

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
        # Enter while the popover is open runs the highlighted command, never a goal.
        if self._popover_open:
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
        elif outcome.action == ACTION_ANSWER:
            # Answers that become transcript turns render via the sync pass;
            # approval answers never reach the transcript, so echo them here.
            if outcome.kind == REQUEST_APPROVAL:
                self._write_conversation(f"you (approval): {text}")
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
        self._refresh_cost()  # live per-goal cost off the run's ledger (no model call)
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
            detail = friendly_provider_error(outcome.error)  # never leak raw API JSON
            self._write_conversation(f"brain (error): the run failed -- {detail}")
        elif outcome.result is None:
            # No execution happened (declined, or cancelled mid-conversation),
            # so no result turn exists; close the thread visibly anyway.
            self._write_conversation(f"(run ended: {outcome.status}; nothing was executed)")
        cost = self._runner.ledger.total_cost() if self._runner is not None else None
        cost_note = "" if cost is None else f" (cost ${cost:.4f})"
        self._write_activity(f"[finished] {outcome.status}{cost_note}")
        self._router.finish_run()
        # Two-tier cost: fold the goal's final cost into the session cumulative. We flip
        # to IDLE first (finish_run above), so _session_total() -- which adds the live
        # goal only while in-flight -- never double-counts the fold. The per-goal
        # counter keeps showing this goal's total until the next goal starts.
        if cost is not None:
            self._goal_cost = cost
            self._session_cost += cost
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
        base = (
            f"[{state.value}] {hint}  |  "
            f"brain={self._models.brain}  hands={self._models.hands}"
        )
        cost = self._cost_segment()
        # The plain buffer is what headless tests assert on; the widget gets a styled
        # render so the cost segment can stand out (and pulse on a change in v0.0.20).
        self._status_text = f"{base}  |  {cost}" if cost else base
        text = Text(base)
        if cost:
            text.append("  |  ", style="dim")
            text.append(cost, style="bold bright_green" if self._cost_pulse else "green")
        self.query_one("#status", Static).update(text)
        # The input box's placeholder tracks what a submit now means (Fix 1).
        self.query_one("#prompt", Input).placeholder = placeholder_for_state(
            state, self._placeholder
        )

    # -- live cost: a two-tier counter (per-goal + session); reads ALREADY-tracked
    # cost off the run's ledger, so the whole path makes ZERO model calls. Relay
    # SHOWS spend and lets the user stop -- it never imposes a cap.

    def _run_in_flight(self) -> bool:
        """Whether a run is live (any non-idle router state) -- drives the 'esc to
        stop' affordance and whether the session rollup includes the live goal."""
        return self._router.state is not InputState.IDLE

    def _cost_segment(self) -> str:
        """The status-line cost text (``""`` when hidden via the toggle). Shows the
        current goal's cost; while a run is in flight it also shows the stop cue."""
        if not self._cost_visible:
            return ""
        cost = f"${self._goal_cost:.4f}"
        return f"{cost} · esc to stop" if self._run_in_flight() else cost

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

    def _cmd_model(self) -> None:
        """Pick a role, then its model (reuses v0.0.16 listing + validation)."""
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
        the live ``/models`` SelectDialog; a ``manual`` provider (OpenRouter) a slug
        TextEntryDialog validated live. On a successful save, ``then`` (if given) is
        scheduled AFTER this dialog tears down -- the chaining seam ``both`` uses to
        run brain then hands in sequence.
        """
        try:
            profile = resolve_provider(provider)
        except ValueError:
            profile = None

        def after_save(ok: bool) -> None:
            if ok and then is not None:
                self.call_after_refresh(then)  # next step, after this dialog dismisses

        if profile is not None and profile.discovery == DISCOVERY_LIST:
            list_fn = self._list_models_fn or provider_list_models
            try:
                ids = list(list_fn(provider))
            except Exception:  # noqa: BLE001 -- no key/network -> empty, handled below
                ids = []

            def on_pick(value, r=role, p=provider) -> None:
                ok, _ = self._save_role_model(r, p, value)
                after_save(ok)

            options = [
                {"title": mid, "value": mid, "category": provider, "on_select": on_pick}
                for mid in ids
            ] or [{"title": "(no models listed -- add a key with /key)", "value": "__none__"}]
            self.push_screen(SelectDialog(title=f"Pick a {role} model ({provider})", options=options))
        else:
            # manual aggregator: a slug field validated live before saving.
            def on_submit(slug, r=role, p=provider):
                ok, note = self._save_role_model(r, p, slug)
                after_save(ok)
                return ok, note

            self.push_screen(TextEntryDialog(
                title=f"{role} model ({provider})",
                label="Type a model slug (validated live before saving):",
                password=False, placeholder="e.g. openai/gpt-4o",
                on_submit=on_submit,
            ))

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
        ok, note = persist_role(
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
        secrets_set_key(provider, key)  # the same v0.0.16 secrets path; value never echoed
        self._on_setup_saved()
        return True, f"stored a key for {provider}"

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
        """Run the provider/model preflight (reusing the CLI logic) in a dialog."""
        rows = self._run_doctor_report()
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
            from relay import cli

            checks = cli._doctor_checks(self._models, None)
            clients = cli._build_provider_clients(checks)
            rows, _ = cli._run_doctor(checks, clients)
            return rows
        except Exception as exc:  # noqa: BLE001 -- never crash the TUI on a preflight
            note = friendly_provider_error(str(exc).splitlines()[0][:120])
            return [{"role": "?", "provider": "?", "model": "?",
                     "status": "FAILED", "note": note}]

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

    def _cmd_assume(self) -> None:
        """Pick the assumption level for this session (a select, not an inline number).

        Each level carries a short description DERIVED from the real dial semantics
        (:func:`relay.config.assumption_summary`), so the text can't drift from what
        the brain is actually instructed to do. The current level is marked.
        """
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

    def _cmd_clear(self) -> None:
        """Clear the visible panes for a fresh session. Guarded: never while a run
        is in flight (also gated by the command's ``enabled`` predicate)."""
        if _run_active(self):
            return
        self._conversation_lines = []
        self._activity_lines = []
        try:
            self.query_one("#conversation", RichLog).clear()
            self.query_one("#activity", RichLog).clear()
        except Exception:  # noqa: BLE001 -- panes not mounted (welcome view)
            pass

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
