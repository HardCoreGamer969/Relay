"""Product-decision firewall: typed escalations (product | tech | mechanical).

Fail-closed: unlabeled questions are treated as ``product`` and never
auto-answered. Tech may auto under permissive dials (1–2); mechanical usually
auto. The assumption dial never overrides a ``product`` class.
"""

from __future__ import annotations

import re

QUESTION_CLASSES = ("product", "tech", "mechanical")
DEFAULT_QUESTION_CLASS = "product"  # fail closed

# Leading markers the hands (or tests) may use instead of a class= attribute.
_CLASS_PREFIX_RE = re.compile(
    r"^\s*(?:"
    r"\[(?P<br>product|tech|mechanical)\]"
    r"|class\s*[:=]\s*(?P<eq>product|tech|mechanical)"
    r")\s*[:\-]?\s*",
    re.IGNORECASE,
)


def normalize_question_class(raw: str | None) -> str:
    """Canonical class; unknown/empty → ``product`` (fail closed)."""
    if raw is None:
        return DEFAULT_QUESTION_CLASS
    value = str(raw).strip().lower()
    if value in QUESTION_CLASSES:
        return value
    return DEFAULT_QUESTION_CLASS


def classify_question(
    content: str,
    *,
    explicit: str | None = None,
) -> tuple[str, str]:
    """Resolve class + cleaned question text.

    Precedence: ``explicit`` (e.g. ``class=`` on the tag) → leading
    ``[tech]`` / ``class: tech`` marker in content → unlabeled ``product``.
    """
    text = content or ""
    if explicit is not None and str(explicit).strip():
        return normalize_question_class(explicit), text.strip()
    match = _CLASS_PREFIX_RE.match(text)
    if match:
        raw = match.group("br") or match.group("eq")
        return normalize_question_class(raw), text[match.end():].strip()
    return DEFAULT_QUESTION_CLASS, text.strip()


def may_auto_answer(question_class: str, assumption_level: str) -> bool:
    """Whether the brain may self-answer (vs must escalate to the user).

    | class       | dial        | auto?                          |
    |-------------|-------------|--------------------------------|
    | product     | any         | never                          |
    | tech        | 1, 2        | yes (permissive)               |
    | tech        | 3+ / auto   | no (ask user)                  |
    | mechanical  | not 5       | yes                            |
    | mechanical  | 5           | no (exact-letter still asks)   |
    """
    cls = normalize_question_class(question_class)
    dial = str(assumption_level).strip().lower()
    if cls == "product":
        return False
    if cls == "mechanical":
        return dial != "5"
    if cls == "tech":
        return dial in ("1", "2")
    return False
