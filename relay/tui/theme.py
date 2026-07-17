"""Shared theme, palette, and welcome-animation constants for the Relay TUI."""

from __future__ import annotations

import random

from relay.bridge import InputState

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

# -- palette: website brand (U2+) with legacy aliases kept for older tests ----
# Source: website/css/style.css. Brain = signal red; hands = dim text; findings
# stay green (success). Cost = warn amber. Cyberpunk ambient flair is NOT used.
W_BG_DEEP = "#000000"
W_BG = "#050505"
W_BG_RAISED = "#0a0a0a"
W_BG_CARD = "#0f0f0f"
W_RED = "#ff0000"
W_RED_BRIGHT = "#ff1a1a"
W_WARN = "#ff6600"
W_TEXT = "#f0f0f0"
W_TEXT_DIM = "#888888"
W_TEXT_MUTED = "#555555"
W_BORDER = "#1a1a1a"

C_BG = W_BG
C_PANEL = W_BG_RAISED
C_CYAN = W_TEXT_DIM          # hands (was neon cyan; remapped to site dim)
C_MAGENTA = W_RED            # brain (was magenta; remapped to site red)
C_GREEN = "#3ee48b"
C_AMBER = W_WARN
C_RED = W_RED_BRIGHT
C_TXT = W_TEXT
C_MUTED = W_TEXT_DIM
C_DIM = W_TEXT_MUTED

# Speaker -> gutter style for the stream (Relay's brain/hands/you distinction).
_ACTOR_STYLES = {
    ACTOR_BRAIN: W_RED,
    ACTOR_HANDS: W_TEXT_DIM,
    ACTOR_YOU: f"bold {W_TEXT}",
}

# The active-step / running spinner (a clean spinner, from the mockup) + plan icons.
# Motion happens ONLY on the active plan step + the mode LED (activity-only).
_SPINNER_FRAMES = ("◍", "◐", "◎", "◑")  # ◍ ◐ ◎ ◑
# Plan-step icons. The "active" step animates through _SPINNER_FRAMES while running;
# its entry here is the RESTING icon shown when motion is off ("off" anim mode, or a
# settled/halted run) -- keep it equal to _SPINNER_FRAMES[0] so the two never diverge.
_PLAN_ICON = {"done": "◉", "active": "◍", "pending": "○", "failed": "✗"}
_SPIN_INTERVAL_S = 0.15   # active-step spinner cadence
_LED_INTERVAL_S = 0.7     # the mode LED's slow "breathing" cadence
