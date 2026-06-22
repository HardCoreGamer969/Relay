"""Plan memory: the brain's within-run knowledge store (v0.06 substrate).

Plan memory is the brain's *reasoning state* for one run -- facts it discovered,
decisions it made (with rationale), user confirmations, and dead ends -- not
telemetry. It lives in-process for a single run and is NOT persisted (it never
touches ``runs.jsonl``). The autonomous planner<->executor loop that reads it is
the NEXT milestone; this module builds only the store and its window-aware
budget/slice/compress machinery.

The key property: memory is **queryable and sliceable**, never a transcript
blob -- because it must shrink to fit any brain's context window, from a 200K
frontier model down to an 8K local one. The same store, the same code, just a
tighter budget. Entries are dual-fidelity (precise ``detail`` for the brain;
plain ``summary`` for humans), the same pattern used elsewhere in Relay.

Design note (snapshot-friendly): ``PlanMemory`` is an append log of immutable
:class:`MemoryEntry` values plus a counter, so a point-in-time copy is cheap
(``to_state`` / ``from_state``). v0.06 does NOT build snapshotting (that is
v0.2) -- it just keeps the shape so snapshotting stays cheap later.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, replace

from relay.config import ModelConfig
from relay.context import DEFAULT_CONTEXT_WINDOW
from relay.models import call_model
from relay.telemetry import Ledger
from relay._utils import estimate_tokens, fit_to_budget

# ``estimate_tokens`` is re-exported here (imported from relay._utils) so existing
# ``from relay.memory import estimate_tokens`` callers keep working after the
# v0.0.32 dedup that moved the implementation to relay._utils.

# --- entry kinds + ranking weights -----------------------------------------

KINDS = ("fact", "decision", "confirmation", "dead_end")

# Kind weighting for relevance (modest vs the keyword-overlap term, so overlap
# dominates and kind/recency break ties). Confirmations (the user said so) and
# decisions rank highest; dead ends still matter (avoid repeating mistakes).
KIND_WEIGHT = {
    "confirmation": 4.0,
    "decision": 3.0,
    "fact": 2.0,
    "dead_end": 2.0,
}
_DEFAULT_KIND_WEIGHT = 1.0

# --- token budgeting knobs (explicit + documented) -------------------------

# Fraction of the brain's context window we allot to plan memory. The rest is
# headroom for the system prompt, the current decision, and an executor report.
MEMORY_BUDGET_FRACTION = 0.5
# Fixed reserve (tokens) carved off the top for that headroom.
RESERVE_TOKENS = 1024
# In ``compacted_context``, the share of the budget kept for verbatim top
# entries; the remainder holds the brain-written summary of the overflow.
VERBATIM_SHARE = 0.75

# --- the three-pool bus knobs (v0.0.29, Stage 1) ---------------------------
# The three pools a :class:`MemoryBus` owns. brain-only (the brain's planning
# memory; the hands never sees it), hands-only (the hands' private scratch; the
# brain never sees it -- written in Stage 1, READ by the hands only in Stage 2),
# and shared (bidirectional, curated: brain->hands directives + hands->brain
# findings; both sides read it).
POOL_BRAIN = "brain"
POOL_HANDS = "hands"
POOL_SHARED = "shared"
POOLS = (POOL_BRAIN, POOL_HANDS, POOL_SHARED)

# Share of the BRAIN's read budget reserved for the shared pool when the brain
# reads brain ∪ shared, so the rendered union stays within the brain window's
# budget (the RESERVE_TOKENS headroom is tuned for a single window). The rest of
# the budget goes to the brain pool. Deliberately small: shared is curated
# directives/findings, not bulk, so it should not crowd out the brain's own pool.
SHARED_READ_FRACTION = 0.25

_WORD_RE = re.compile(r"[a-z0-9]+")

# --- near-duplicate suppression knobs (v0.0.21) ----------------------------
# Across a stuck step's reviews the brain restates the same finding many times
# ("main() lacks the input loop" / "...lacks the input loop implementation" /
# "...does not contain the required input loop"). Stored verbatim, these
# rewordings dominate the keyword-ranked slice fed back into the next review and
# reinforce the loop. So a new entry that near-restates a recent same-kind entry
# is suppressed. Conservative by construction: same-kind only, a bounded recent
# window, and a novelty guard that keeps a genuinely-new superset entry.
_DEDUP_WINDOW = 12          # recent same-kind entries to scan
_DEDUP_MIN_TOKENS = 5       # below this many content tokens, only an exact match dedups
_DEDUP_CONTAINMENT = 0.7    # shared-token share of the SMALLER entry to call it a restatement
_DEDUP_MAX_EXTRA = 0.4      # ...AND the larger entry adds <=40% brand-new tokens (else it is new info)

# Function words that don't carry a fact's identity; dropped before comparison so
# rewordings ("lacks X" vs "does not contain the required X") still match on
# content tokens. Negations ("not"/"no") are deliberately KEPT (they flip meaning).
_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "to", "of", "in", "on", "for", "with",
    "that", "this", "as", "at", "by", "from", "into", "is", "are", "be", "was",
    "were", "does", "do", "did", "has", "have", "had", "it", "its", "their",
})

_SUMMARY_SYSTEM = (
    "You are compacting a coding agent's plan memory to fit a small context "
    "window. Compress the given older memory entries into the densest possible "
    "note that preserves the facts, decisions, and dead ends still relevant to "
    "the query. Keep it plain ASCII. No preamble -- output only the compacted note."
)


@dataclass(frozen=True)
class MemoryEntry:
    """One unit of within-run knowledge, dual-fidelity.

    ``detail`` is the precise form (for the brain's reasoning and for executors);
    ``summary`` is the short human-facing form. ``provenance`` records where it
    came from (a step index, an executor report, a user confirmation).
    ``created_at`` is a monotonic ordinal so order is stable.
    """

    id: str
    kind: str
    provenance: str
    detail: str
    summary: str
    created_at: int
    tags: list[str] = field(default_factory=list)


@dataclass
class PlanMemory:
    """Append-mostly store of :class:`MemoryEntry` values for one run.

    In-memory only (no persistence, no DB). Genuinely-new knowledge is never
    dropped; the one deliberate exception (v0.0.21) is **near-duplicate
    suppression** -- a new entry that merely restates a RECENT same-kind entry
    (the brain rewording the same finding across a stuck step's reviews) is not
    appended, so a handful of restatements can't dominate the keyword-ranked
    slice fed back into the next review and reinforce a loop. That is a feature,
    not silent loss: the knowledge is already present. See
    :func:`texts_near_duplicate` for the conservative test (same-kind, recent
    window, novelty guard so a superset that ADDS information is kept).
    """

    entries: list[MemoryEntry] = field(default_factory=list)
    counter: int = 0  # next monotonic ordinal; part of the snapshot state

    # -- mutation ----------------------------------------------------------

    def add(self, entry: MemoryEntry) -> MemoryEntry:
        """Append ``entry``, assigning a monotonic ``created_at`` (and ``id`` if blank)."""
        ordinal = self.counter
        self.counter += 1
        stored = replace(entry, id=entry.id or f"m{ordinal}", created_at=ordinal)
        self.entries.append(stored)
        return stored

    def remember(
        self,
        kind: str,
        detail: str,
        summary: str,
        *,
        provenance: str = "",
        tags: list[str] | None = None,
    ) -> MemoryEntry | None:
        """Build a :class:`MemoryEntry` and :meth:`add` it -- UNLESS it merely
        restates a recent same-kind entry, in which case it is suppressed and
        ``None`` is returned (the knowledge is already stored; see the class note
        on near-duplicate suppression). Genuinely-new content is always appended.
        """
        if self._is_near_duplicate(kind, detail):
            return None
        return self.add(
            MemoryEntry(
                id="",
                kind=kind,
                provenance=provenance,
                detail=detail,
                summary=summary,
                created_at=0,
                tags=list(tags or []),
            )
        )

    def _is_near_duplicate(self, kind: str, detail: str) -> bool:
        """Whether ``detail`` restates a RECENT same-kind entry (bounded scan).

        Only same-kind entries, only the last :data:`_DEDUP_WINDOW` of them, via
        the shared :func:`texts_near_duplicate` test -- so the check is cheap and
        cannot suppress an entry just because something unrelated long ago looked
        similar.
        """
        examined = 0
        for entry in reversed(self.entries):
            if entry.kind != kind:
                continue
            examined += 1
            if examined > _DEDUP_WINDOW:
                break
            if texts_near_duplicate(entry.detail, detail):
                return True
        return False

    # -- snapshot shape (cheap point-in-time copy; the v0.2 hook) -----------

    def to_state(self) -> dict:
        """Capture full state as a plain JSON-able value."""
        return {"counter": self.counter, "entries": [asdict(e) for e in self.entries]}

    @classmethod
    def from_state(cls, state: dict) -> "PlanMemory":
        """Reconstruct from :meth:`to_state` output (round-trips equal)."""
        entries = [MemoryEntry(**dict(e)) for e in state.get("entries", [])]
        return cls(entries=entries, counter=int(state.get("counter", len(entries))))

    # -- retrieval ---------------------------------------------------------

    def _ranked(self, query: str) -> list[MemoryEntry]:
        terms = _terms(query)
        max_ord = max((e.created_at for e in self.entries), default=0)
        scored = [(_score(e, terms, max_ord), e) for e in self.entries]
        # Sort by score desc, then newer-first as a stable, deterministic tiebreak.
        scored.sort(key=lambda se: (se[0], se[1].created_at), reverse=True)
        return [entry for _, entry in scored]

    def relevant(self, query: str, *, budget_tokens: int) -> list[MemoryEntry]:
        """Top entries by relevance to ``query`` that fit within ``budget_tokens``.

        Budget-bounded, NOT a fixed count: on a large budget this may be every
        entry; on a tiny one (an 8K-window-derived budget) just a few. Packs in
        rank order, keeping each entry that still fits (so a single oversized
        high-rank entry doesn't starve smaller relevant ones).
        """
        if budget_tokens <= 0:
            return []
        out: list[MemoryEntry] = []
        used = 0
        for entry in self._ranked(query):
            cost = entry_tokens(entry)
            if used + cost <= budget_tokens:
                out.append(entry)
                used += cost
        return out

    def compacted_context(
        self,
        query: str,
        *,
        budget_tokens: int,
        client=None,
        models: ModelConfig | None = None,
        ledger: Ledger | None = None,
    ) -> str:
        """Budget-bounded context string: top entries verbatim, overflow summarized.

        Compress, don't truncate: when genuinely-relevant memory exceeds the
        budget after ranking, the lower-priority/older overflow is summarized by
        one ``call_model("brain", ...)`` (attributed in telemetry) so the
        *knowledge* survives even when the *detail* can't. If everything fits, no
        summarization call is made. If the summarizer fails, it degrades to a
        hard-trim with a visible note -- never raises. The result fits the budget.
        """
        if budget_tokens <= 0 or not self.entries:
            return ""
        ranked = self._ranked(query)

        # If the whole working set fits verbatim in the full budget, just use it.
        full = _pack(ranked, budget_tokens, contiguous=False)
        if len(full) == len(ranked):
            return fit_to_budget("\n".join(_render_entry(e) for e in full), budget_tokens)

        # Overflow exists: verbatim top share + a summary of the remainder.
        verbatim_cap = int(budget_tokens * VERBATIM_SHARE)
        verbatim = _pack(ranked, verbatim_cap, contiguous=True)
        overflow = ranked[len(verbatim):]
        verbatim_text = "\n".join(_render_entry(e) for e in verbatim)
        used = sum(entry_tokens(e) for e in verbatim)
        summary_budget = max(0, budget_tokens - used)

        summary_block = self._summarize_overflow(
            overflow, query, summary_budget, client=client, models=models, ledger=ledger
        )
        out = f"{verbatim_text}\n\n{summary_block}" if verbatim_text else summary_block
        return fit_to_budget(out, budget_tokens)

    def _summarize_overflow(
        self, overflow, query, summary_budget, *, client, models, ledger
    ) -> str:
        n = len(overflow)
        plural = "y" if n == 1 else "ies"
        overflow_text = "\n".join(_render_entry(e) for e in overflow)
        try:
            prompt = (
                f"Query in focus: {query}\n\n"
                f"Older plan-memory entries to compact (keep what matters to the query):\n"
                f"{overflow_text}"
            )
            result = call_model(
                "brain",
                [
                    {"role": "system", "content": _SUMMARY_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                models=models,
                ledger=ledger,
                client=client,
                max_tokens=max(16, summary_budget),
            )
            summary = (result.text or "").strip()
            if not summary:
                raise ValueError("empty summary")
            block = f"[compacted memory: {n} older entr{plural} summarized]\n{summary}"
            return fit_to_budget(block, summary_budget)
        except Exception as exc:  # noqa: BLE001 -- degrade gracefully, never crash
            # Hard-trim fallback with a visible (non-silent) note about the loss.
            return f"[compacted memory unavailable: {n} older entr{plural} omitted ({exc.__class__.__name__})]"


# --- module-level helpers ---------------------------------------------------


def _render_entry(entry: MemoryEntry) -> str:
    """The precise, brain-facing rendering of an entry (also used for sizing)."""
    tags = (" #" + " #".join(entry.tags)) if entry.tags else ""
    return f"[{entry.kind} | {entry.provenance}] {entry.detail}{tags}"


def entry_tokens(entry: MemoryEntry) -> int:
    return estimate_tokens(_render_entry(entry))


def memory_budget(
    window: int, *, fraction: float = MEMORY_BUDGET_FRACTION, reserve: int = RESERVE_TOKENS
) -> int:
    """Token budget for plan memory derived from the brain's context window."""
    return max(0, int(window * fraction) - reserve)


def small_window_warning(memory: PlanMemory, window: int) -> str | None:
    """A warning when ``window`` is too small for the current working set, else None.

    "Too small" = the memory budget can't fit even the single smallest entry, so
    memory can't be meaningfully sliced. Relay saying so beats degrading silently.
    """
    if not memory.entries:
        return None
    budget = memory_budget(window)
    smallest = min(entry_tokens(e) for e in memory.entries)
    if budget < smallest:
        return (
            f"context window {window} is too small for plan memory "
            f"(budget {budget} tokens < smallest entry {smallest}); declare a larger "
            "window via RELAY_BRAIN_CONTEXT or use a brain model with a bigger window."
        )
    return None


def _terms(query: str) -> set[str]:
    return set(_WORD_RE.findall((query or "").lower()))


def _normalize(text: str) -> str:
    """Whitespace/case-normalized form, for exact-duplicate comparison."""
    return " ".join((text or "").lower().split())


def _content_tokens(text: str) -> set[str]:
    """Content (non-stopword) word tokens of ``text``, for similarity."""
    return {t for t in _WORD_RE.findall((text or "").lower()) if t not in _STOPWORDS}


def texts_near_duplicate(a: str, b: str) -> bool:
    """True iff ``a`` and ``b`` are restatements of the same thing (deterministic).

    Either exact after whitespace/case normalization, OR -- for entries with
    enough content -- a high shared-token share (the overlap as a fraction of the
    SMALLER entry) together with only a small amount of brand-new content on the
    larger side. That second clause is the **novelty guard**: a genuine superset
    that ADDS information (the larger entry is mostly new tokens) is NOT a
    duplicate, so real new knowledge is never dropped. Symmetric, and shared by
    memory's suppression and the orchestrator's repetition breaker so the two can
    never diverge.
    """
    na, nb = _normalize(a), _normalize(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    ta, tb = _content_tokens(a), _content_tokens(b)
    if len(ta) < _DEDUP_MIN_TOKENS or len(tb) < _DEDUP_MIN_TOKENS:
        return False
    overlap = len(ta & tb)
    if not overlap:
        return False
    smaller, larger = sorted((len(ta), len(tb)))
    containment = overlap / smaller
    extra_ratio = (larger - overlap) / larger
    return containment >= _DEDUP_CONTAINMENT and extra_ratio <= _DEDUP_MAX_EXTRA


def _score(entry: MemoryEntry, terms: set[str], max_ord: int) -> float:
    haystack = " ".join(
        [entry.detail, entry.summary, entry.kind, " ".join(entry.tags)]
    ).lower()
    overlap = sum(1 for t in terms if t in haystack)
    recency = (entry.created_at + 1) / (max_ord + 1)  # 0..1, newer is higher
    kind_w = KIND_WEIGHT.get(entry.kind, _DEFAULT_KIND_WEIGHT)
    return overlap * 10.0 + kind_w + recency


def _pack(entries, cap: int, *, contiguous: bool) -> list[MemoryEntry]:
    """Greedily select entries within ``cap`` tokens.

    ``contiguous=True`` stops at the first entry that doesn't fit (a clean top
    prefix, for the verbatim/overflow split). ``contiguous=False`` skips an
    over-budget entry and keeps trying smaller ones (max knowledge under budget).
    """
    out: list[MemoryEntry] = []
    used = 0
    for entry in entries:
        cost = entry_tokens(entry)
        if used + cost > cap:
            if contiguous:
                break
            continue
        out.append(entry)
        used += cost
    return out


# --- the three-pool memory bus (v0.0.29, Stage 1) --------------------------

# Conservative per-pool read budget when the windows haven't been resolved yet
# (a bare bus, or the pre-execution session before :meth:`MemoryBus.configure_budgets`).
_DEFAULT_POOL_BUDGET = memory_budget(DEFAULT_CONTEXT_WINDOW)


@dataclass
class MemoryBus:
    """Three :class:`PlanMemory` pools behind one façade -- the v0.0.29 substrate.

    The single brain-only ``PlanMemory`` of v0.06 becomes three pools, each
    budgeted to its OWN actor's context window instead of forcing one budget on
    both roles:

      - ``brain`` -- the brain's planning/reasoning memory; the hands never sees it.
      - ``hands`` -- the hands' private scratch; the brain never sees it. Written in
        Stage 1 (e.g. nothing routes here yet by default); the hands READS it only
        in Stage 2 (deferred).
      - ``shared`` -- bidirectional and curated: brain->hands directives (authoritative
        decisions/confirmations) and hands->brain findings. Both sides read it, so it
        is budgeted to fit the SMALLER window.

    Each pool is an UNMODIFIED :class:`PlanMemory` (its ``remember``/``relevant``/
    ``compacted_context``/dedup/ranking are pool-agnostic and reused verbatim), so a
    per-pool counter, within-pool near-duplicate suppression, and the existing
    snapshot shape all carry over for free. The bus only routes writes (``pool=``)
    and composes budget-bounded read UNIONS per actor.

    Read budgets are set by :meth:`configure_budgets` once both windows are resolved;
    until then they default to the conservative single-window budget so a bare bus
    (tests, the pre-execution session) still reads sensibly.
    """

    brain: PlanMemory = field(default_factory=PlanMemory)
    hands: PlanMemory = field(default_factory=PlanMemory)
    shared: PlanMemory = field(default_factory=PlanMemory)
    brain_budget: int = _DEFAULT_POOL_BUDGET
    hands_budget: int = _DEFAULT_POOL_BUDGET
    shared_budget: int = _DEFAULT_POOL_BUDGET

    # -- budgeting ---------------------------------------------------------

    def configure_budgets(self, *, brain_window: int, hands_window: int) -> "MemoryBus":
        """Set each pool's read budget from its actor's window (chainable).

        brain pool -> the brain window; hands pool -> the hands window; shared ->
        ``min(brain, hands)`` because it renders into BOTH contexts and must fit the
        smaller. Reuses :func:`memory_budget` unchanged (the split is here, above it).
        """
        self.brain_budget = memory_budget(brain_window)
        self.hands_budget = memory_budget(hands_window)
        self.shared_budget = memory_budget(min(brain_window, hands_window))
        return self

    def _shared_read_cap(self, budget_tokens: int) -> int:
        """The share of a brain read budget given to shared, bounded so the rendered
        brain ∪ shared union stays within ``budget_tokens`` (the union-cap mechanism)."""
        if budget_tokens <= 0:
            return 0
        cap = min(self.shared_budget, int(budget_tokens * SHARED_READ_FRACTION))
        return max(0, min(cap, budget_tokens))

    # -- routing -----------------------------------------------------------

    def pool(self, name: str) -> PlanMemory:
        """The sub-store named ``name`` (one of :data:`POOLS`)."""
        if name == POOL_BRAIN:
            return self.brain
        if name == POOL_HANDS:
            return self.hands
        if name == POOL_SHARED:
            return self.shared
        raise KeyError(f"unknown memory pool: {name!r}")

    def remember(
        self,
        kind: str,
        detail: str,
        summary: str,
        *,
        pool: str = POOL_BRAIN,
        provenance: str = "",
        tags: list[str] | None = None,
    ) -> MemoryEntry | None:
        """Write an entry into ``pool`` (default the brain pool, so existing brain
        write sites keep their behavior). Returns the stored entry, or ``None`` if it
        was suppressed as a within-pool near-duplicate."""
        return self.pool(pool).remember(kind, detail, summary, provenance=provenance, tags=tags)

    @property
    def entries(self) -> list[MemoryEntry]:
        """All entries across the three pools (brain, then hands, then shared).

        A read-only UNION view for duck-typed consumers (the debug bundle, the CLI
        activity feed) that only iterate entries. Pool-specific assertions should use
        ``bus.pool(name).entries`` instead."""
        return [*self.brain.entries, *self.hands.entries, *self.shared.entries]

    # -- reads (budget-bounded unions per actor) ---------------------------

    def brain_context(
        self,
        query: str,
        budget_tokens: int,
        *,
        client=None,
        models: ModelConfig | None = None,
        ledger: Ledger | None = None,
    ) -> str:
        """The brain's read: ``brain ∪ shared``, ranked per-pool, fit to ``budget_tokens``.

        ``budget_tokens`` is the brain-window budget. It is split into a shared cap
        (:meth:`_shared_read_cap`) and the brain-pool remainder, so the rendered union
        is guaranteed ``<= budget_tokens`` (the mechanized cap, not just an assertion)."""
        if budget_tokens <= 0:
            return ""
        shared_cap = self._shared_read_cap(budget_tokens)
        brain_cap = max(0, budget_tokens - shared_cap)
        parts: list[str] = []
        if brain_cap > 0 and self.brain.entries:
            text = self.brain.compacted_context(
                query, budget_tokens=brain_cap, client=client, models=models, ledger=ledger
            )
            if text:
                parts.append(text)
        if shared_cap > 0 and self.shared.entries:
            text = self.shared.compacted_context(
                query, budget_tokens=shared_cap, client=client, models=models, ledger=ledger
            )
            if text:
                parts.append(text)
        # Final guard: the per-pool caps sum to budget_tokens, but joining them adds a
        # separator, so _fit the union to be STRICTLY within budget (the mechanized cap).
        return fit_to_budget("\n".join(parts), budget_tokens)

    def hands_context(
        self,
        query: str,
        budget_tokens: int,
        *,
        client=None,
        models: ModelConfig | None = None,
        ledger: Ledger | None = None,
    ) -> str:
        """The hands' read (Stage 1): the SHARED pool ONLY -- never the brain pool, and
        not yet its own hands pool (Stage 2 is deferred). Capped to fit the hands window
        (``min(budget_tokens, shared_budget)``) so the curated directives/findings the
        brain put in shared reach the hands without leaking the brain's reasoning."""
        cap = min(budget_tokens, self.shared_budget)
        if cap <= 0 or not self.shared.entries:
            return ""
        return self.shared.compacted_context(
            query, budget_tokens=cap, client=client, models=models, ledger=ledger
        )

    # -- snapshot shape (three nested per-pool snapshots) ------------------

    def to_state(self) -> dict:
        """Versioned envelope wrapping each pool's :meth:`PlanMemory.to_state`."""
        return {
            "v": 3,
            "pools": {
                POOL_BRAIN: self.brain.to_state(),
                POOL_HANDS: self.hands.to_state(),
                POOL_SHARED: self.shared.to_state(),
            },
        }

    @classmethod
    def from_state(cls, state: dict) -> "MemoryBus":
        """Reconstruct from :meth:`to_state`. A legacy single-pool snapshot (no
        ``"pools"`` key) loads its entries into the brain pool, so old state restores."""
        pools = state.get("pools")
        if not pools:
            return cls(brain=PlanMemory.from_state(state))
        return cls(
            brain=PlanMemory.from_state(pools.get(POOL_BRAIN, {})),
            hands=PlanMemory.from_state(pools.get(POOL_HANDS, {})),
            shared=PlanMemory.from_state(pools.get(POOL_SHARED, {})),
        )
