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
import platform
import random
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Container, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Select, Static

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
    default_config,
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
from relay.secrets import resolve_key, set_key as secrets_set_key
from relay.store import CONFIG_VERSION, load_config, save_config
from relay.transcript import Turn

# How often the conversation pane catches up with the (append-only) transcript.
_SYNC_INTERVAL_S = 0.2
# Bounded wait when joining the worker on quit -- never hang the exit.
_JOIN_TIMEOUT_S = 5.0

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
    "what's next?",
    "what are we building?",
    "what should we tackle?",
    "what needs doing?",
    "point me at something...",
)

# The states where the engine is ACTIVELY generating (the model is genuinely
# running). The slash popover is suppressed only here -- every other state (idle and
# the awaiting-user states) accepts a slash command (see ``_slash_allowed``).
_GENERATING_STATES = (InputState.PLANNING, InputState.EXECUTING)

# State-aware placeholders: the one box's PURPOSE changes with what the engine is
# waiting for, so the prompt should say what a submit now means. Short.
_STATE_PLACEHOLDERS = {
    InputState.AWAITING_REACTION: "React to the plan (approve, or ask for changes)...",
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

# -- the cyberpunk palette (v0.0.30): the single source of truth for the stream's
# styling, mirroring the agreed mockup. Near-black background, neon accents.
# Relay's improvement over a single-agent stream is the brain/hands/findings split:
#   brain = magenta, hands = cyan, findings = green, you = bright magenta;
#   cost = amber, done = green, active = cyan. Cyberpunk == palette + activity-only
#   spinners/LED ONLY -- no CRT/scanlines/glow/ambient motion (it's a tool).
C_BG = "#06090e"
C_PANEL = "#080d14"
C_CYAN = "#34d9ee"
C_MAGENTA = "#e879f9"
C_GREEN = "#3ee48b"
C_AMBER = "#ffcf4d"
C_RED = "#ff6b6b"
C_TXT = "#d3e3ef"
C_MUTED = "#8aa0b3"
C_DIM = "#5a7187"

# Speaker -> gutter style for the stream (Relay's brain/hands/you distinction).
_ACTOR_STYLES = {ACTOR_BRAIN: C_MAGENTA, ACTOR_HANDS: C_CYAN, ACTOR_YOU: f"bold {C_MAGENTA}"}

# The active-step / running spinner (a clean spinner, from the mockup) + plan icons.
# Motion happens ONLY on the active plan step + the mode LED (activity-only).
_SPINNER_FRAMES = ("◍", "◐", "◎", "◑")  # ◍ ◐ ◎ ◑
# Plan-step icons. The "active" step animates through _SPINNER_FRAMES while running;
# its entry here is the RESTING icon shown when motion is off ("off" anim mode, or a
# settled/halted run) -- keep it equal to _SPINNER_FRAMES[0] so the two never diverge.
_PLAN_ICON = {"done": "◉", "active": "◍", "pending": "○", "failed": "✗"}
_SPIN_INTERVAL_S = 0.15   # active-step spinner cadence
_LED_INTERVAL_S = 0.7     # the mode LED's slow "breathing" cadence


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
    if kind in ("scoping_question", "elicitation", "clarify"):
        return ACTOR_BRAIN, f"asks: {p.get('question', msg)}"
    if kind == "user_reacted":
        return ACTOR_YOU, f"reacted: {p.get('reaction', msg)}"
    if kind == "rejected":
        return ACTOR_YOU, "rejected the plan"
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
    # ``EngineRunner`` records its terminal outcome before invoking the UI's
    # ``on_finished`` callback.  The worker thread can remain alive for a tiny
    # tail while that callback returns, but it cannot mutate the transcript or
    # execute more work once ``outcome`` is set.  Treat that settled tail as
    # inactive so a user who interrupts, stops, and immediately runs ``/clear``
    # does not hit a silent no-op race.
    return (
        runner is not None
        and getattr(runner, "is_running", False)
        and getattr(runner, "outcome", None) is None
    )


def _parse_inline_command(text: str) -> tuple[str, str] | None:
    """Parse ``/name arg...`` into ``(name, arg)``; ``None`` if not a slash command.

    Only the v0.0.28 inline-arg commands (``/queue`` / ``/redirect``) use this; every
    other slash command stays argument-free and runs via the popover."""
    if not text.startswith("/"):
        return None
    parts = text[1:].split(None, 1)
    if not parts:
        return None
    return parts[0], (parts[1].strip() if len(parts) > 1 else "")


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

    def _on_paste(self, event) -> None:
        """Capture a multi-line paste IN FULL.

        Textual's ``Input._on_paste`` keeps only ``event.text.splitlines()[0]`` --
        silently dropping every line after the first. That corrupts a pasted
        multi-line goal/spec (Relay sees only line one). We insert the WHOLE pasted
        text instead. A newline WITHIN a paste is content, never a submit: the paste
        arrives as one ``Paste`` event (not a stream of Enter keypresses), so this
        does not submit and does not touch the explicit Enter-to-submit path for
        typed input.

        ``prevent_default()`` is essential: Textual dispatches an event to EVERY
        matching handler in the MRO, so without it the base ``Input._on_paste``
        would still run after this one and re-append the truncated first line.
        """
        text = event.text
        if text:
            selection = self.selection
            if selection.is_empty:
                self.insert_text_at_cursor(text)
            else:
                self.replace(text, *selection)
        event.prevent_default()  # suppress the base (first-line-only) paste handler
        event.stop()

    def on_key(self, event) -> None:
        app = self.app
        # While the slash popover is open, up/down move the highlight and esc closes it.
        if getattr(app, "_popover_open", False):
            if event.key == "down":
                app._popover_move(1); event.prevent_default(); event.stop()
            elif event.key == "up":
                app._popover_move(-1); event.prevent_default(); event.stop()
            elif event.key == "escape":
                app._popover_close(); event.prevent_default(); event.stop()
            return
        # Otherwise up/down are the ONE unified recall-and-edit affordance: walk the
        # input history (goals, steers, queued items) into the field for editing.
        if event.key == "up":
            recalled = app._recall_older()
            if recalled is not None:
                self.value = recalled
                self.cursor_position = len(self.value)
            event.prevent_default(); event.stop()
        elif event.key == "down":
            recalled = app._recall_newer()
            if recalled is not None:
                self.value = recalled
                self.cursor_position = len(self.value)
            event.prevent_default(); event.stop()


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
    Command("cwd", "Working dir", "Show / set the session working directory", "ops",
            run=lambda app: app._cmd_cwd(), enabled=lambda app: not _run_active(app)),
    Command("redirect", "Redirect", "Steer now: redirect the work (or /redirect <input>)", "ops",
            run=lambda app: app._open_inline_dialog("redirect")),
    Command("queue", "Queue", "Do this next: queue input (or /queue <input>)", "ops",
            run=lambda app: app._open_inline_dialog("queue")),
    Command("cost", "Cost", "Session + per-goal spend; toggle / reset the counter", "ops",
            run=lambda app: app._cmd_cost()),
    Command("log", "Log", "Export a debug log (.md) to share when reporting an issue", "ops",
            run=lambda app: app._cmd_log()),
    Command("clear", "Clear", "Clear the stream + start a fresh session", "ops",
            run=lambda app: app._cmd_clear(), enabled=lambda app: not _run_active(app)),
]


class RelayTuiApp(App):
    """A welcome screen that hands off to a single live stream chat over the engine."""

    TITLE = "Relay"

    CSS = """
    Screen { layout: vertical; background: #06090e; }

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

    /* -- the working state: ONE live scrolling stream (v0.0.30) -- */
    #working { height: 1fr; layout: vertical; display: none; }
    #stream {
        height: 1fr;
        padding: 0 1;
        background: #06090e;
    }
    #stream .plan { margin: 1 0 1 0; padding: 0 0 0 2; }
    #stream .stream-row { width: 1fr; }
    #status { height: 1; padding: 0 1; background: #080d14; }

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
        self._plan_block = None             # the mounted plan widget (updated in place)
        # The rendered stream rows (Rich Text / str), in order -- the headless mirror
        # of what the single stream shows (so tests can pin speaker/finding styling
        # without depending on Textual widget internals). The live plan block updates
        # in place and is NOT in here (its state is _plan_steps).
        self._stream_rendered: list = []
        self._spin_frame = 0                # active-step spinner frame
        self._spin_timer = None             # active while a step is executing
        self._led_on = True                 # the mode LED's breathing phase
        self._led_timer = None

    # -- layout ---------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Container(id="welcome"):
            with Vertical(id="welcome-inner"):
                yield Static(RELAY_WORDMARK, id="brand")
                yield Static(self._greeting, id="greeting")
                yield Static(self._indicator_text, id="indicator")
                yield Static("esc to cancel  ·  ctrl+q to quit", id="hint")
        with Container(id="working"):
            # ONE live scrolling stream: conversation, the inline live plan, tool
            # calls, findings, and verdicts all interleaved in the order they happen
            # (the v0.0.30 replacement for the old Conversation/Activity two-pane).
            yield VerticalScroll(id="stream")
            yield Static(id="status")
        yield Static(id="command-popover")
        yield PromptInput(id="prompt", placeholder=self._placeholder)

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
        # Inline-arg commands (/queue <input>, /redirect <input>) take precedence over
        # the popover -- they carry an argument the popover can't. They also work mid-run
        # (the popover is suppressed while executing, but Enter still routes here).
        inline = _parse_inline_command(event.value)
        if inline is not None and inline[0] in ("queue", "redirect") and inline[1]:
            event.input.value = ""
            self._popover_close()
            (self._do_queue if inline[0] == "queue" else self._do_redirect)(inline[1])
            self._update_status()
            return
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
        # the conversation -- the stream keeps the human story (the headline turn +
        # the surfaced assumptions, from the plan_proposed event via
        # _render_plan_split_buffer) while the numbered steps render as the inline
        # live PLAN block. Other asks (decision/approval) still surface their prompt
        # when it adds detail beyond the last transcript turn (e.g. the approval command).
        if request.kind != REQUEST_REACTION:
            last_turn_text = self._last_synced_turn_text()
            if request.prompt.strip() != (last_turn_text or "").strip():
                for line in present_prompt(request.prompt).splitlines():
                    self._write_conversation(f"brain: {line}" if line.strip() else "")
        self._update_status()

    def _handle_event(self, event: Event) -> None:
        """One engine event: phase changes steer the router; everything else renders
        INLINE in the single live stream (conversation, the live plan, tool calls,
        findings, verdicts), interleaved in the order it happens.

        Everything shown here is read from the event the engine ALREADY emitted --
        the render path makes no model call (proven by the zero-new-tokens guard).
        """
        if event.kind == EVENT_PHASE:
            # Internal routing only -- not surfaced as a stream line.
            self._router.set_phase(event.payload.get("phase", ""))
        else:
            self._render_event(event)
        self._sync_transcript()
        self._refresh_cost()  # live per-goal cost off the run's ledger (no model call)
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
                self._write_conversation(f"brain (assumes): {assumption}")

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
        """Mount one row/widget into the stream and keep it pinned to the live edge."""
        stream = self._stream()
        if stream is None:
            return
        try:
            stream.mount(widget)
            stream.scroll_end(animate=False)
        except Exception:  # noqa: BLE001 -- teardown race; the buffer already has it
            pass

    def _push_row(self, renderable, *, classes: str = "stream-row") -> None:
        """Record a stream row (the headless mirror) and mount it into the stream."""
        self._stream_rendered.append(renderable)
        self._mount_stream(Static(renderable, classes=classes))

    def _row(self, gutter: str, body: str, *, gutter_style: str = "", body_style: str = "") -> None:
        """Build + push one labeled stream row (the mockup's gutter + body line)."""
        text = Text()
        text.append(f"{gutter:<6}" if gutter else " " * 6, style=gutter_style or C_DIM)
        text.append(body, style=body_style or C_TXT)
        self._push_row(text)

    def _record_activity(self, actor: str | None, line: str) -> None:
        """Append to the activity BUFFER only (the test/debug record) -- no stream row.
        Used for the bespoke-form events + the dim detail lines, so the stream shows
        their inline form (or nothing) rather than a duplicate generic row."""
        self._activity_lines.append(f"{actor} | {line}" if actor else line)

    def _write_conversation(self, line: str) -> None:
        """Record a conversation line (buffer) and render it as a stream row, colored
        by speaker (you = bright magenta, brain = magenta, system = muted)."""
        self._conversation_lines.append(line)
        if not line.strip():
            self._push_row("")  # a blank spacer between runs
            return
        head = line.split(None, 1)[0]
        rest = line.split(None, 1)[1] if " " in line else ""
        if head == "you":
            self._row("you", rest, gutter_style=f"bold {C_MAGENTA}", body_style=C_TXT)
        elif head == "brain":
            self._row("brain", rest, gutter_style=C_MAGENTA, body_style=C_TXT)
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
        """A compact tool-call stream line: ``▸ read cli.py · 78 lines``."""
        text = Text()
        text.append("  ▸ ", style=C_CYAN)
        text.append(label, style=C_MUTED)
        if result:
            text.append(f"  · {result[:60]}", style=C_DIM)
        self._push_row(text)

    def _stream_finding(self, note: str) -> None:
        """A finding (v0.0.29 hands->brain channel) renders as a distinct GREEN line."""
        text = Text()
        text.append("  ⚠ finding", style=f"bold {C_GREEN}")
        text.append(f" → {note}", style=C_MUTED)
        self._push_row(text)

    def _stream_verdict(self, index, verdict: str) -> None:
        """A compact review verdict line: ``review ✓ accept · step 04``."""
        accepted = "accept" in (verdict or "").lower()
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
        """(Re)build the live plan from an emitted step list and mount/refresh its
        block. The initial plan starts every step pending; a revision keeps already-
        settled steps and replaces the pending tail with the new pending steps."""
        if revised and self._plan_steps:
            kept = [s for s in self._plan_steps if s["status"] != "pending"]
            self._plan_steps = kept + [{"instruction": s, "status": "pending"} for s in steps]
        else:
            self._plan_steps = [{"instruction": s, "status": "pending"} for s in steps]
            self._plan_block = None  # a fresh plan -> a fresh block (prior plan stays in scroll-back)
        if self._plan_block is None:
            self._plan_block = Static(classes="plan")
            self._mount_stream(self._plan_block)
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

    def _plan_render(self) -> None:
        """Render the live plan block from its step states: ◉ done (dim) / ◍ active
        (spinner, bright) / ○ pending (dimmer). Updates the SAME widget in place."""
        block = self._plan_block
        if block is None:
            return
        total = len(self._plan_steps)
        text = Text()
        text.append(f"plan · {total} step{'s' if total != 1 else ''}", style=C_DIM)
        for i, step in enumerate(self._plan_steps):
            status = step["status"]
            icon = (_SPINNER_FRAMES[self._spin_frame % len(_SPINNER_FRAMES)]
                    if status == "active" else _PLAN_ICON.get(status, "○"))
            icon_style = {"done": C_GREEN, "active": C_CYAN, "failed": C_RED}.get(status, C_DIM)
            body_style = {"active": f"bold {C_TXT}", "done": C_MUTED}.get(status, C_DIM)
            text.append("\n")
            text.append(f"{icon} ", style=icon_style)
            text.append(f"{i + 1:02d} ", style=C_DIM)
            text.append(step["instruction"], style=body_style)
        try:
            block.update(text)
        except Exception:  # noqa: BLE001 -- teardown race
            pass
        if not any(s["status"] == "active" for s in self._plan_steps):
            self._stop_spin()

    def _reset_plan(self) -> None:
        """Drop the live-plan state (a new goal starts a fresh plan; prior plans stay
        frozen in scroll-back). Stops the active-step spinner."""
        self._plan_steps = []
        self._plan_block = None
        self._stop_spin()

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
        """The status-line MODE word: WORKING while the engine generates, INTERRUPTED
        at the interrupt prompt, AWAITING YOU when it is the user's turn (idle or an
        awaiting-reaction/decision/approval ask)."""
        if state is InputState.INTERRUPTED:
            return "INTERRUPTED"
        if state in (InputState.PLANNING, InputState.EXECUTING):
            return "WORKING"
        return "AWAITING YOU"

    def _step_segment(self) -> str:
        """``step N/M`` from the live plan (active step, else settled count); '' if no plan."""
        total = len(self._plan_steps)
        if not total:
            return ""
        active = next((i for i, s in enumerate(self._plan_steps) if s["status"] == "active"), None)
        if active is not None:
            n = active + 1
        else:
            n = sum(1 for s in self._plan_steps if s["status"] in ("done", "failed"))
        return f"step {n}/{total}"

    def _update_status(self) -> None:
        """The status line: a breathing mode LED + WORKING/AWAITING YOU · step N/M ·
        cost (amber) · cwd (cyan) · the model pairing (dim) · queue (magenta), with a
        right hint. The plain ``_status_text`` mirror carries the same facts (what the
        headless tests assert on); the widget gets the styled render."""
        state = self._router.state
        mode = self._mode_word(state)
        step = self._step_segment()
        cost = self._cost_segment()
        cwd = self._cwd_segment()
        queued = f"queued: {len(self._session.queue)}" if self._session.queue else ""

        segs = [mode]
        for seg in (step, cost, cwd):
            if seg:
                segs.append(seg)
        segs.append(f"brain {self._models.brain}")
        segs.append(f"hands {self._models.hands}")
        if queued:
            segs.append(queued)
        self._status_text = "  ·  ".join(segs)

        working = mode == "WORKING"
        led_color = C_GREEN if working else C_MAGENTA
        text = Text()
        text.append("● " if self._led_on else "○ ", style=led_color)  # the breathing LED
        text.append(mode, style=f"bold {led_color}")
        if step:
            text.append("  ·  ", style=C_DIM)
            text.append(step, style=C_CYAN)
        if cost:
            text.append("  ·  ", style=C_DIM)
            text.append(cost, style=("bold " + C_AMBER) if self._cost_pulse else C_AMBER)
        if cwd:
            text.append("  ·  ", style=C_DIM)
            text.append(cwd, style=C_CYAN)
        text.append("  ·  ", style=C_DIM)
        text.append(f"brain {self._models.brain} · hands {self._models.hands}", style=C_DIM)
        if queued:
            text.append("  ·  ", style=C_DIM)
            text.append(queued, style=C_MAGENTA)
        hint = "esc interrupt · /queue" if self._run_in_flight() else "enter send · ↑ recall · /queue"
        text.append("    ", style=C_DIM)
        text.append(hint, style=C_DIM)
        try:
            self.query_one("#status", Static).update(text)
        except Exception:  # noqa: BLE001 -- not mounted / teardown race
            pass
        # The input box's placeholder tracks what a submit now means (Fix 1).
        try:
            self.query_one("#prompt", Input).placeholder = placeholder_for_state(
                state, self._placeholder
            )
        except Exception:  # noqa: BLE001 -- not mounted
            pass

    def _cwd_segment(self) -> str:
        """The status-line working-dir segment, shown when the sticky working dir
        has moved off the launch root (so the user can SEE where Relay will work).
        At the launch root the default is obvious, so nothing extra is shown."""
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
        """The status-line cost text (``""`` when hidden via the toggle). Shows the
        current goal's cost; while a run is in flight it also shows the stop cue."""
        if not self._cost_visible:
            return ""
        cost = f"${self._goal_cost:.4f}"
        if not self._run_in_flight():
            return cost
        return f"{cost} · stopping..." if self._stopping else f"{cost} · esc to stop"

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

    def _cmd_cwd(self) -> None:
        """Show the current session working dir and let the user set a new one.

        The working dir is session-sticky: a set here persists across subsequent
        goals (until changed) -- the next goal operates from it, not the launch
        root. Guarded to non-running states (the command's ``enabled`` predicate)."""
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
        """Show session + per-goal spend, and offer toggle / reset. Dialog-driven (no
        inline args); cost is already tracked so opening this makes NO model call.
        Relay SHOWS spend and lets YOU decide when to stop -- it never caps."""
        session = self._session_total()
        options = [
            {"title": f"Session total: ${session:.4f}", "value": "__session__", "category": "spend",
             "description": "Cumulative across all goals since launch or last reset"},
            {"title": f"This goal: ${self._goal_cost:.4f}", "value": "__goal__", "category": "spend",
             "description": "The current goal's cost (or the last goal's, while idle)"},
            {"title": f"Live counter: {'on' if self._cost_visible else 'off'}", "value": "__toggle__",
             "category": "actions", "description": "Show/hide the status-line per-goal counter",
             "on_select": (lambda v: self._toggle_cost_counter())},
            {"title": "Reset session total", "value": "__reset__", "category": "actions",
             "description": "Zero the session figure (a deliberate break; leaves the goal "
                            "counter and any run untouched)",
             "on_select": (lambda v: self._reset_session_cost())},
        ]
        self.push_screen(SelectDialog(
            title="Cost (Relay shows spend; you decide when to stop)", options=options))

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
            transcript_lines = (
                [format_turn(t) for t in runner.transcript.turns]
                if runner is not None else []
            )
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

    def action_cancel_run(self) -> None:
        """esc = INTERRUPT, not teardown. A running run halts at the clean boundary and
        lands at the interrupt prompt (session intact); a SECOND esc (already
        interrupted) is STOP -- abandon the plan, keep the session."""
        if self._router.state is InputState.INTERRUPTED:
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
