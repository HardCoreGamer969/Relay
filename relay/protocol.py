"""The action protocol.

Relay models express actions as plain text tags that Relay parses itself --
never via a provider's native tool/function-calling API. This is what keeps
*every* model (including ones with no function-calling support) in the
comparison set. This module defines the tag format and a tolerant parser.

Supported tags::

    <thinking>...</thinking>                 optional; captured, not executed
    <read path="..."/>
    <list path="..."/>                       path optional, defaults to "."
    <grep pattern="..." path="..."/>
    <edit path="...">...full file content...</edit>
    <bash>...command...</bash>
    <done>...short summary...</done>         ends the (sub)loop
    <plan><step>...</step>...</plan>         the brain's ordered plan (v0.04)
    <abort>reason</abort>                    the brain: goal is unreachable (v0.04)
    <blocked>reason</blocked>                the executor: stuck on this step (v0.04)
    <question>...</question>                 the executor: needs info to proceed (v0.06)
    <finding>...</finding>                   the executor: surface a bug/discovery (v0.0.29)

``<question>`` is distinct from ``<blocked>``: a question is mid-step (the brain
answers it or escalates, then the executor continues), whereas ``<blocked>`` ends
the step. ``<finding>`` is distinct from BOTH: it is a non-blocking note the hands
emits to tell the planner about a bug / security issue / wrong assumption -- it does
NOT end the step and the hands does NOT wait for an answer (it is recorded to the
shared memory pool and the hands continues working).

``<done>`` is context-dependent in v0.04: from the **executor** it means *this
step* is complete (not the whole task); the task completes when the plan is
exhausted. The ``<plan>``/``<abort>`` tags come from the brain (planner) and
``<blocked>`` from the hands (executor).

A message that contains no valid action and no ``<done>`` is a *parse failure*,
surfaced via :attr:`ParseResult.is_parse_failure` so the loop can correct it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Action kinds the parser can produce.
KINDS = (
    "read", "list", "grep", "edit", "bash", "done", "plan", "abort", "blocked", "question",
    "write", "glob", "apply_patch", "webfetch", "mkdir", "finding",
)


@dataclass
class Action:
    """A single action parsed from a model message.

    Fields are populated per ``kind``:
      - ``read`` / ``list``: ``path``
      - ``grep``: ``pattern`` + ``path``
      - ``edit`` / ``write``: ``path`` + ``content`` (full new file contents)
      - ``bash`` / ``apply_patch``: ``content`` (the command / the patch envelope)
      - ``glob``: ``pattern`` + ``path`` (the base dir to match under)
      - ``webfetch``: ``url``
      - ``done`` / ``abort`` / ``blocked`` / ``finding``: ``content`` (the summary/reason/note)
      - ``plan``: ``steps`` (ordered step instructions)
    """

    kind: str
    path: str | None = None
    pattern: str | None = None
    content: str | None = None
    steps: list[str] | None = None
    url: str | None = None


@dataclass
class ParseResult:
    """The actions (in document order) and any thinking found in a message."""

    actions: list[Action] = field(default_factory=list)
    thinking: list[str] = field(default_factory=list)

    @property
    def is_parse_failure(self) -> bool:
        """True when no executable action (including ``<done>``) was found."""
        return not self.actions

    @property
    def is_done(self) -> bool:
        return any(a.kind == "done" for a in self.actions)

    def first(self, kind: str) -> Action | None:
        """Return the first action of ``kind`` in document order, or None."""
        for action in self.actions:
            if action.kind == kind:
                return action
        return None

    @property
    def plan_steps(self) -> list[str] | None:
        """Ordered step instructions if a ``<plan>`` was emitted, else None."""
        plan = self.first("plan")
        return plan.steps if plan is not None else None


_ATTR_RE = re.compile(r"""(\w+)\s*=\s*(["'])(.*?)\2""", re.DOTALL)
_THINKING_RE = re.compile(r"<thinking>(.*?)</thinking>", re.DOTALL)
_PLAN_RE = re.compile(r"<plan>(.*?)</plan>", re.DOTALL)
_STEP_RE = re.compile(r"<step>(.*?)</step>", re.DOTALL)
_ABORT_RE = re.compile(r"<abort>(.*?)</abort>", re.DOTALL)
_BLOCKED_RE = re.compile(r"<blocked>(.*?)</blocked>", re.DOTALL)
_QUESTION_RE = re.compile(r"<question>(.*?)</question>", re.DOTALL)
_FINDING_RE = re.compile(r"<finding>(.*?)</finding>", re.DOTALL)
_EDIT_RE = re.compile(r"""<edit\s+([^>]*?)>(.*?)</edit>""", re.DOTALL)
_WRITE_RE = re.compile(r"""<write\s+([^>]*?)>(.*?)</write>""", re.DOTALL)
_APPLY_PATCH_RE = re.compile(r"<apply_patch>(.*?)</apply_patch>", re.DOTALL)
_BASH_RE = re.compile(r"<bash>(.*?)</bash>", re.DOTALL)
_DONE_RE = re.compile(r"<done>(.*?)</done>", re.DOTALL)
_SELF_CLOSING_RE = re.compile(r"<(read|list|grep|glob|webfetch|mkdir)\b([^>]*?)/>", re.DOTALL)


def _attrs(text: str) -> dict[str, str]:
    """Parse ``name="value"`` attributes (single or double quoted)."""
    return {m.group(1): m.group(3) for m in _ATTR_RE.finditer(text)}


def _strip_block_newlines(content: str) -> str:
    """Drop a single leading and trailing newline.

    Models commonly lay block bodies out on their own lines::

        <edit path="x">
        ...content...
        </edit>

    so we trim one wrapping newline on each side while preserving the rest of
    the content (including interior and intentional surrounding whitespace).
    """
    if content.startswith("\n"):
        content = content[1:]
    if content.endswith("\n"):
        content = content[:-1]
    return content


def _mask(text: str, spans: list[tuple[int, int]]) -> str:
    """Blank out ``spans`` with spaces, preserving length so indices stay valid.

    Used to hide block-tag bodies (``<edit>``/``<bash>``/``<done>``) before
    scanning for self-closing tags, so tag-like text *inside* a body is not
    parsed as a separate action.
    """
    if not spans:
        return text
    chars = list(text)
    for start, end in spans:
        for i in range(start, end):
            chars[i] = " "
    return "".join(chars)


def parse(text: str) -> ParseResult:
    """Parse a model message into ordered actions plus captured thinking.

    Tolerant of surrounding prose: only recognized tags are extracted, in the
    order they appear in the message. Block-tag bodies are masked cumulatively
    as they are consumed, so a tag nested inside another tag's body (e.g. a
    ``<read.../>`` written inside an ``<edit>`` body, or an ``<edit>`` inside a
    ``<plan>``) is not double-parsed as its own action.
    """
    if not text:
        return ParseResult()

    placed: list[tuple[int, Action]] = []
    thinking: list[str] = []
    masked = text

    def consume(regex: re.Pattern[str], on_match) -> None:
        nonlocal masked
        spans: list[tuple[int, int]] = []
        for m in regex.finditer(masked):
            on_match(m)
            spans.append((m.start(), m.end()))
        masked = _mask(masked, spans)

    consume(_THINKING_RE, lambda m: thinking.append(m.group(1).strip()))

    def _add_plan(m: re.Match[str]) -> None:
        steps = [s.group(1).strip() for s in _STEP_RE.finditer(m.group(1))]
        placed.append((m.start(), Action(kind="plan", steps=[s for s in steps if s])))

    # Plan first, so <step>/<edit>/etc. inside a plan body are not parsed loose.
    consume(_PLAN_RE, _add_plan)
    # apply_patch next: its body is a patch envelope that may ADD a file whose content
    # contains tag-like lines (e.g. +<edit ...>), so mask it early before loose scans.
    consume(
        _APPLY_PATCH_RE,
        lambda m: placed.append((m.start(), Action(kind="apply_patch", content=_strip_block_newlines(m.group(1))))),
    )
    # write next: its body is arbitrary file content that may contain tag-like lines,
    # so mask it early before the loose-tag scans below (same reasoning as edit).
    def _add_write(m: re.Match[str]) -> None:
        path = _attrs(m.group(1)).get("path")
        if path:
            placed.append(
                (m.start(), Action(kind="write", path=path, content=_strip_block_newlines(m.group(2))))
            )

    consume(_WRITE_RE, _add_write)
    consume(_ABORT_RE, lambda m: placed.append((m.start(), Action(kind="abort", content=m.group(1).strip()))))
    consume(_BLOCKED_RE, lambda m: placed.append((m.start(), Action(kind="blocked", content=m.group(1).strip()))))

    def _add_edit(m: re.Match[str]) -> None:
        path = _attrs(m.group(1)).get("path")
        if path:
            placed.append(
                (m.start(), Action(kind="edit", path=path, content=_strip_block_newlines(m.group(2))))
            )

    consume(_EDIT_RE, _add_edit)
    consume(_BASH_RE, lambda m: placed.append((m.start(), Action(kind="bash", content=m.group(1).strip()))))
    consume(_DONE_RE, lambda m: placed.append((m.start(), Action(kind="done", content=m.group(1).strip()))))
    # After edit/bash/done so a <question> mentioned inside one of their bodies
    # (e.g. an <edit> file body) is masked first and not parsed as a loose action.
    consume(_QUESTION_RE, lambda m: placed.append((m.start(), Action(kind="question", content=m.group(1).strip()))))
    # <finding> (v0.0.29) last among the block tags, same masking rationale as
    # <question>: a <finding> written inside an <edit>/<bash> body is masked first.
    consume(_FINDING_RE, lambda m: placed.append((m.start(), Action(kind="finding", content=m.group(1).strip()))))

    # Self-closing tags last, scanned over the fully-masked text.
    for m in _SELF_CLOSING_RE.finditer(masked):
        kind = m.group(1)
        attrs = _attrs(m.group(2))
        if kind == "read":
            if attrs.get("path"):
                placed.append((m.start(), Action(kind="read", path=attrs["path"])))
        elif kind == "list":
            placed.append((m.start(), Action(kind="list", path=attrs.get("path", "."))))
        elif kind == "grep":
            if attrs.get("pattern") and attrs.get("path"):
                placed.append(
                    (m.start(), Action(kind="grep", pattern=attrs["pattern"], path=attrs["path"]))
                )
        elif kind == "glob":
            if attrs.get("pattern"):
                placed.append(
                    (m.start(), Action(kind="glob", pattern=attrs["pattern"], path=attrs.get("path", ".")))
                )
        elif kind == "webfetch":
            if attrs.get("url"):
                placed.append((m.start(), Action(kind="webfetch", url=attrs["url"])))
        elif kind == "mkdir":
            if attrs.get("path"):
                placed.append((m.start(), Action(kind="mkdir", path=attrs["path"])))

    placed.sort(key=lambda item: item[0])
    return ParseResult(actions=[action for _, action in placed], thinking=thinking)


# --- shared tag-content helpers (v0.0.32: deduplicated from planner/conversation) ---
#
# Several modules (planner.py, conversation.py) extract the text content of a
# simple ``<name>...</name>`` tag. These helpers centralize that logic so the
# three copies don't drift. Each is a thin regex wrapper -- no behavioral change.

_TAG_CONTENT_RE_CACHE: dict[str, re.Pattern[str]] = {}


def tag_content(name: str, text: str) -> str | None:
    """Extract the first ``<name>...</name>`` tag's stripped content, or ``None``.

    A shared helper for modules that need to pull a single tag's text (e.g. the
    brain's ``<verdict>``, ``<reaction>``, ``<scope>``). Compiled regexes are
    cached per tag name for reuse.
    """
    pattern = _TAG_CONTENT_RE_CACHE.get(name)
    if pattern is None:
        pattern = re.compile(rf"<{name}>(.*?)</{name}>", re.DOTALL)
        _TAG_CONTENT_RE_CACHE[name] = pattern
    match = pattern.search(text or "")
    return match.group(1).strip() if match else None


def tag_contents(name: str, text: str) -> list[str]:
    """Extract all ``<name>...</name>`` tags' stripped, non-empty contents.

    A shared helper for modules that need multiple tags (e.g. the brain's
    ``<ask>`` / ``<assume>`` / ``<record>`` tags).
    """
    pattern = _TAG_CONTENT_RE_CACHE.get(name)
    if pattern is None:
        pattern = re.compile(rf"<{name}>(.*?)</{name}>", re.DOTALL)
        _TAG_CONTENT_RE_CACHE[name] = pattern
    return [
        m.group(1).strip() for m in pattern.finditer(text or "") if m.group(1).strip()
    ]
