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
    <done>...short summary...</done>         ends the loop

A message that contains no valid action and no ``<done>`` is a *parse failure*,
surfaced via :attr:`ParseResult.is_parse_failure` so the loop can correct it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Action kinds the parser can produce.
KINDS = ("read", "list", "grep", "edit", "bash", "done")


@dataclass
class Action:
    """A single action parsed from a model message.

    Fields are populated per ``kind``:
      - ``read`` / ``list``: ``path``
      - ``grep``: ``pattern`` + ``path``
      - ``edit``: ``path`` + ``content`` (full new file contents)
      - ``bash``: ``content`` (the command)
      - ``done``: ``content`` (the summary)
    """

    kind: str
    path: str | None = None
    pattern: str | None = None
    content: str | None = None


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


_ATTR_RE = re.compile(r"""(\w+)\s*=\s*(["'])(.*?)\2""", re.DOTALL)
_THINKING_RE = re.compile(r"<thinking>(.*?)</thinking>", re.DOTALL)
_EDIT_RE = re.compile(r"""<edit\s+([^>]*?)>(.*?)</edit>""", re.DOTALL)
_BASH_RE = re.compile(r"<bash>(.*?)</bash>", re.DOTALL)
_DONE_RE = re.compile(r"<done>(.*?)</done>", re.DOTALL)
_SELF_CLOSING_RE = re.compile(r"<(read|list|grep)\b([^>]*?)/>", re.DOTALL)


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
    order they appear in the message.
    """
    if not text:
        return ParseResult()

    placed: list[tuple[int, Action]] = []
    thinking: list[str] = []
    consumed: list[tuple[int, int]] = []

    for m in _THINKING_RE.finditer(text):
        thinking.append(m.group(1).strip())
        consumed.append((m.start(), m.end()))

    for m in _EDIT_RE.finditer(text):
        path = _attrs(m.group(1)).get("path")
        if path:
            placed.append(
                (m.start(), Action(kind="edit", path=path, content=_strip_block_newlines(m.group(2))))
            )
        consumed.append((m.start(), m.end()))

    for m in _BASH_RE.finditer(text):
        placed.append((m.start(), Action(kind="bash", content=m.group(1).strip())))
        consumed.append((m.start(), m.end()))

    for m in _DONE_RE.finditer(text):
        placed.append((m.start(), Action(kind="done", content=m.group(1).strip())))
        consumed.append((m.start(), m.end()))

    # Mask block bodies so self-closing tags inside them are not double-parsed.
    masked = _mask(text, consumed)
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

    placed.sort(key=lambda item: item[0])
    return ParseResult(actions=[action for _, action in placed], thinking=thinking)
