"""The continuous transcript: one human-facing thread across planning + execution.

Before this milestone the user met the brain twice -- once in the planning
conversation (``plan_conversationally``) and again, separately, when a mid-run
product decision escalated (``user_decision``). Two "the brain is asking me
something" modes. This module fuses them into ONE thread: a single
:class:`Transcript` that spans the planning dialogue AND the execution
escalations, so a mid-run question is just the next turn in the same
conversation.

Two stores now both compact, and they must NOT drift apart:

- The **transcript** (here) is human-facing, chronological, append-only, and the
  SOURCE OF TRUTH for what was said and decided. It compacts toward
  *readability* (:func:`compact_transcript` / :func:`render_for_brain`).
- **Plan memory** (``relay.memory``) is brain-facing, relevance-queryable,
  dense, and the brain's working extract. It compacts toward token-efficiency.

**Plan memory is derived FROM the transcript, never the reverse.** A user-facing
decision is recorded to the transcript FIRST (authoritative), then derived into a
linked :class:`~relay.memory.MemoryEntry` whose ``provenance`` references the
turn it came from (:func:`record_decision`). When the two disagree, the
transcript wins and memory is what gets re-derived.

Scope guards (this is prompt B of B): the transcript is in-process for one
``relay run``'s arc (planning -> execution -> escalations -> result). It is
snapshot-shaped (``to_state`` / ``from_state``) so cross-session persistence and
scroll-back are cheap later, but neither persistence nor a scrollable TUI is
built here -- the plain CLI renders it as printable history. No streaming.

Readability vs. density are different jobs: this module DOES NOT reuse plan
memory's ``compacted_context`` (dense machine notes would wreck scroll-back). Its
compaction keeps recent turns verbatim and folds older ones into a readable
narrative a human can still follow.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace

from relay.config import ModelConfig
from relay.memory import PlanMemory, estimate_tokens
from relay.models import call_model
from relay.telemetry import Ledger

# Phases a conversational turn can belong to (open set; these are the ones Relay
# emits today). The transcript holds CONVERSATION, not an execution-event dump:
# granular tool calls stay in the Event stream / run log, never here.
PHASES = (
    "planning",     # scope question / elicitation Q&A
    "proposal",     # the brain's (re-)proposed plan, plain language
    "reaction",     # the user's free-form reaction to a proposal
    "commit",       # the user committed the plan (a decision)
    "escalation",   # the brain put a product decision to the user, mid-run
    "decision",     # the user's answer to an escalation (a decision)
    "result",       # the brain's high-level "here's where things ended" turn
    "summary",      # a readable narrative folding older turns (compaction output)
)

# Recent turns kept verbatim by compaction; older turns fold into one narrative.
DEFAULT_KEEP_RECENT = 6
# Share of a brain-read budget spent on recent verbatim turns; the rest holds the
# readable narrative of the older remainder. (Same split idea as plan memory, but
# the overflow becomes readable prose, not dense notes.)
_VERBATIM_SHARE = 0.75

# Distinct from plan memory's dense ``_SUMMARY_SYSTEM``: this asks for a readable
# narrative a person can follow on scroll-back, NOT the densest possible note.
_NARRATIVE_SYSTEM = (
    "You are folding the EARLIER part of a planning-and-execution conversation into "
    "a SHORT READABLE NARRATIVE so a person can still follow the story after the old "
    "turns scroll away. Write 2-4 plain-prose sentences, past tense, naming who said "
    "what in order -- for example: 'Earlier, you asked for a todo app, the brain "
    "proposed a Flask backend, you changed auth to Google-only, then committed.' This "
    "is human scroll-back, NOT dense machine notes and NOT a bullet list. Plain ASCII, "
    "no preamble -- output only the narrative."
)


@dataclass(frozen=True)
class Turn:
    """One conversational turn in the continuous thread.

    ``speaker`` is ``"user"`` or ``"brain"``; ``phase`` is one of :data:`PHASES`;
    ``text`` is the human-readable content; ``created_at`` is a monotonic ordinal
    so order is stable; ``refs`` optionally links to plan steps / memory entries.
    """

    id: str
    speaker: str
    phase: str
    text: str
    created_at: int
    refs: list[str] = field(default_factory=list)


@dataclass
class Transcript:
    """Ordered, append-only thread of :class:`Turn` values for one run.

    In-process only (like plan memory -- no persistence here), but snapshot-shaped
    (:meth:`to_state` / :meth:`from_state` round-trip) so persistence and
    cross-session scroll-back are cheap to add later.
    """

    turns: list[Turn] = field(default_factory=list)
    counter: int = 0  # next monotonic ordinal; part of the snapshot state

    # -- mutation ----------------------------------------------------------

    def add(self, turn: Turn) -> Turn:
        """Append ``turn``, assigning a monotonic ``created_at`` (and ``id`` if blank)."""
        ordinal = self.counter
        self.counter += 1
        stored = replace(turn, id=turn.id or f"t{ordinal}", created_at=ordinal)
        self.turns.append(stored)
        return stored

    def record(self, speaker: str, phase: str, text: str, *, refs: list[str] | None = None) -> Turn:
        """Convenience: build a :class:`Turn` and :meth:`add` it."""
        return self.add(
            Turn(id="", speaker=speaker, phase=phase, text=text, created_at=0, refs=list(refs or []))
        )

    # -- reads -------------------------------------------------------------

    def recent(self, n: int) -> list[Turn]:
        """The most recent ``n`` turns in chronological order (empty if ``n<=0``)."""
        return self.turns[-n:] if n > 0 else []

    def is_empty(self) -> bool:
        return not self.turns

    # -- snapshot shape (cheap point-in-time copy; the persistence hook) ----

    def to_state(self) -> dict:
        """Capture full state as a plain JSON-able value."""
        return {"counter": self.counter, "turns": [asdict(t) for t in self.turns]}

    @classmethod
    def from_state(cls, state: dict) -> "Transcript":
        """Reconstruct from :meth:`to_state` output (round-trips equal)."""
        turns = [Turn(**dict(t)) for t in state.get("turns", [])]
        return cls(turns=turns, counter=int(state.get("counter", len(turns))))

    def copy(self) -> "Transcript":
        """A cheap, independent copy (via the snapshot shape)."""
        return Transcript.from_state(self.to_state())


# --- transcript-first, memory-derived (the core invariant) ------------------


def record_decision(
    transcript: Transcript,
    memory: PlanMemory | None,
    *,
    text: str,
    detail: str,
    summary: str,
    kind: str = "decision",
    phase: str = "decision",
    speaker: str = "user",
    refs: list[str] | None = None,
) -> tuple[Turn, "object | None"]:
    """Record a user-facing decision: TRANSCRIPT FIRST, then derive a memory entry.

    This is the one path both planning-commits and escalation-resolutions go
    through. It appends an authoritative ``decision``/``commit`` turn to the
    transcript FIRST, then -- when ``memory`` is given -- derives a linked
    :class:`~relay.memory.MemoryEntry` whose ``provenance`` is ``transcript:<id>``,
    pointing back at that turn. The transcript is the source of truth; the memory
    entry is the derived, re-derivable extract. Returns ``(turn, entry_or_None)``.

    Purely-internal brain facts (executor outcomes, dead ends) are NOT decisions
    and should still go straight to memory -- they are not conversation turns.
    """
    turn = transcript.record(speaker, phase, text, refs=list(refs or []))
    entry = None
    if memory is not None:
        entry = memory.remember(kind, detail, summary, provenance=f"transcript:{turn.id}")
    return turn, entry


# --- readability-preserving compaction (distinct from memory's) -------------


def render_for_brain(
    transcript: Transcript,
    *,
    budget_tokens: int,
    client=None,
    models: ModelConfig | None = None,
    ledger: Ledger | None = None,
) -> str:
    """A window-bounded, READABLE rendering of the thread for the brain to read.

    Recent turns stay verbatim; if they overflow ``budget_tokens`` the older
    remainder is folded into a readable narrative (one ``call_model("brain", ...)``,
    attributed in telemetry). If the whole thread fits, no narration call is made.
    The result is guaranteed to fit the budget. Used so a mid-run escalation can be
    phrased as a continuation of the conversation so far, not a context-less popup.
    """
    turns = transcript.turns
    if not turns or budget_tokens <= 0:
        return ""

    verbatim_all = "\n".join(_render_turn(t) for t in turns)
    if estimate_tokens(verbatim_all) <= budget_tokens:
        return verbatim_all  # whole thread fits -- no narration needed

    verbatim_cap = int(budget_tokens * _VERBATIM_SHARE)
    recent = _pack_recent(turns, verbatim_cap)
    older = turns[: len(turns) - len(recent)]
    recent_text = "\n".join(_render_turn(t) for t in recent)
    narrative_budget = max(0, budget_tokens - estimate_tokens(recent_text))
    narrative = (
        _narrate(older, budget_tokens=narrative_budget, client=client, models=models, ledger=ledger)
        if older
        else ""
    )
    parts = [p for p in (narrative, recent_text) if p]
    return _fit("\n\n".join(parts), budget_tokens)


def compact_transcript(
    transcript: Transcript,
    *,
    keep_recent: int = DEFAULT_KEEP_RECENT,
    budget_tokens: int | None = None,
    client=None,
    models: ModelConfig | None = None,
    ledger: Ledger | None = None,
) -> Transcript:
    """Fold older turns into ONE readable narrative turn; keep recent turns verbatim.

    The post-execution "auto-compact" pass: it produces the durable, scroll-back
    record a continued session would resume from. The narrative is prose a human
    can still follow (NOT plan memory's dense notes). Returns a NEW transcript;
    the original is left untouched. Degrades gracefully -- a failing summarizer
    falls back to a readable, noted brief rather than crashing.
    """
    turns = transcript.turns
    if len(turns) <= keep_recent:
        return transcript.copy()  # nothing to fold

    split = len(turns) - keep_recent
    older, recent = turns[:split], turns[split:]
    narrative = _narrate(older, budget_tokens=budget_tokens, client=client, models=models, ledger=ledger)

    out = Transcript()
    out.add(Turn(id="", speaker="brain", phase="summary", text=narrative, created_at=0,
                 refs=[t.id for t in older]))
    # Keep recent turns' ORIGINAL ids so a memory entry's provenance ("transcript:tN")
    # still resolves against the compacted record (the summary turn refs the folded
    # older ids). ``add`` preserves a non-empty id and only the ordinal is re-stamped.
    for turn in recent:
        out.add(Turn(id=turn.id, speaker=turn.speaker, phase=turn.phase, text=turn.text,
                     created_at=0, refs=list(turn.refs)))
    return out


# --- module-level helpers ---------------------------------------------------


def _render_turn(turn: Turn) -> str:
    """The readable rendering of a turn (also used for sizing)."""
    return f"{turn.speaker} ({turn.phase}): {turn.text}".strip()


def turn_tokens(turn: Turn) -> int:
    return estimate_tokens(_render_turn(turn))


def _pack_recent(turns: list[Turn], cap: int) -> list[Turn]:
    """Take the most-recent turns (from the end) that fit ``cap``, chronological.

    Always keeps at least the single most-recent turn (``_fit`` trims it if even
    that overflows), so a brain read never comes back empty.
    """
    out: list[Turn] = []
    used = 0
    for turn in reversed(turns):
        cost = turn_tokens(turn)
        if used + cost > cap and out:
            break
        out.append(turn)
        used += cost
        if used >= cap:
            break
    out.reverse()
    return out


def _narrate(
    turns: list[Turn],
    *,
    budget_tokens: int | None = None,
    client=None,
    models: ModelConfig | None = None,
    ledger: Ledger | None = None,
) -> str:
    """Fold ``turns`` into a readable narrative (one brain call), bounded + graceful.

    On any summarizer failure (or an empty result) this degrades to a deterministic,
    readable, NOTED brief built from the turns themselves -- never raises.
    """
    if not turns:
        return ""
    rendered = "\n".join(_render_turn(t) for t in turns)
    # Explicit None check (not truthiness): budget 0 must NOT fall back to 512.
    max_tokens = max(64, budget_tokens) if budget_tokens is not None else 512
    try:
        result = call_model(
            "brain",
            [
                {"role": "system", "content": _NARRATIVE_SYSTEM},
                {"role": "user", "content":
                    "Earlier conversation turns to fold into a short readable narrative:\n" + rendered},
            ],
            models=models,
            ledger=ledger,
            client=client,
            max_tokens=max_tokens,
        )
        text = (result.text or "").strip()
        if not text:
            raise ValueError("empty narrative")
    except Exception as exc:  # noqa: BLE001 -- degrade gracefully, never crash
        text = _fallback_narrative(turns, note=exc.__class__.__name__)
    return _fit(text, budget_tokens)


def _fallback_narrative(turns: list[Turn], *, note: str = "", max_turns: int = 12) -> str:
    """A deterministic, still-readable narrative when the summarizer is unavailable.

    Keeps the older turns' content (more verbatim) and NOTES that the automatic
    summary was unavailable -- the graceful-degradation contract. Self-bounded
    (caps how many turns it spells out) so it stays readable even with no token
    budget to trim it; ``_narrate``'s ``_fit`` still applies when a budget is given.
    """
    shown, omitted = turns[-max_turns:], max(0, len(turns) - max_turns)
    brief = "; ".join(f"{t.speaker} {t.phase}: {_one_line(t.text)}" for t in shown)
    if omitted:
        brief = f"({omitted} earlier turn(s) omitted); " + brief
    prefix = f"[readable summary unavailable: {note}] " if note else ""
    return f"{prefix}Earlier in the conversation -- {brief}."


def _one_line(text: str, *, cap: int = 120) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= cap else flat[: cap - 3] + "..."


def _fit(text: str, budget: int | None) -> str:
    """Guarantee the string fits the token budget (char-trim). ``None`` = no cap."""
    if budget is None or estimate_tokens(text) <= budget:
        return text
    return text[: max(0, budget * 4)]
