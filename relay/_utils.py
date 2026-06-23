"""Shared utility helpers (v0.0.32: deduplicated from memory.py and transcript.py).

These small helpers were duplicated across :mod:`relay.memory` and
:mod:`relay.transcript` with identical logic. Centralizing them here keeps the
two stores from drifting on the token-estimation or budget-fitting heuristic.
"""

from __future__ import annotations


def estimate_tokens(text: str) -> int:
    """Model-agnostic token estimate (~chars/4). Exactness isn't required;
    staying under the cap is, and this never under-counts to zero."""
    return max(1, (len(text) + 3) // 4)


def fit_to_budget(text: str, budget: int | None) -> str:
    """Guarantee the string fits the token budget (char-trim). ``None`` = no cap.

    Uses :func:`estimate_tokens` to check the budget; if the text exceeds it,
    trims to ``budget * 4`` characters (the same chars/4 heuristic). This is the
    final guard after the budget-aware slicing/compression in memory and
    transcript -- it ensures the result NEVER overflows the budget.
    """
    if budget is None or estimate_tokens(text) <= budget:
        return text
    return text[: max(0, budget * 4)]
