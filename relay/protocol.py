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
    <question class="product|tech|mechanical">...</question>
                                             the executor: needs info to proceed (v0.06);
                                             optional class= for the product-decision
                                             firewall (B2). Unlabeled → product (fail closed).
    <finding>...</finding>                   the executor: surface a bug/discovery (v0.0.29)

``<question>`` is distinct from ``<blocked>``: a question is mid-step (the brain
answers it or escalates, then the executor continues), whereas ``<blocked>`` ends
the step. ``class`` on ``<question>`` (or a leading ``[tech]`` / ``class: tech``
marker in the body) feeds the product-decision firewall. ``<finding>`` is distinct
from BOTH: it is a non-blocking note the hands emits to tell the planner about a
bug / security issue / wrong assumption -- it does NOT end the step and the hands
does NOT wait for an answer (it is recorded to the shared memory pool and the hands
continues working).

``<done>`` is context-dependent in v0.04: from the **executor** it means *this
step* is complete (not the whole task); the task completes when the plan is
exhausted. The ``<plan>``/``<abort>`` tags come from the brain (planner) and
``<blocked>`` from the hands (executor).

A message that contains no valid action and no ``<done>`` is a *parse failure*,
surfaced via :attr:`ParseResult.is_parse_failure` so the loop can correct it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# Action kinds the parser can produce.
KINDS = (
    "read", "list", "grep", "edit", "bash", "done", "plan", "abort", "blocked", "question",
    "write", "glob", "apply_patch", "webfetch", "mkdir", "finding", "tool",
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
      - ``question``: ``content`` + optional ``question_class`` (``product``/``tech``/``mechanical``)
      - ``plan``: ``steps`` (ordered step instructions)
    """

    kind: str
    path: str | None = None
    pattern: str | None = None
    content: str | None = None
    steps: list[str] | None = None
    url: str | None = None
    # Generic registry-backed calls use ``tool_name`` + JSON ``arguments``.
    # Legacy actions keep their historical strongly-named fields above.
    tool_name: str | None = None
    arguments: dict | None = None
    eol: str | None = None
    # Product-decision firewall (B2): class= on <question>, when present.
    question_class: str | None = None


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


_ATTR_RE = re.compile(r"""([A-Za-z_]\w*)\s*=\s*(["'])(.*?)\2""", re.DOTALL)
_STEP_RE = re.compile(r"<step(?:\s[^>]*)?>(.*?)</step>", re.DOTALL)

_BLOCK_KINDS = {
    "thinking", "plan", "apply_patch", "write", "abort", "edit", "bash",
    "question", "finding", "done", "blocked", "tool",
}
_SELF_CLOSING_KINDS = {"read", "list", "grep", "glob", "webfetch", "mkdir"}
_NAME_RE = re.compile(r"/?([A-Za-z_][\w.-]*)")


def _xml_unescape(text: str) -> str:
    """Decode only XML's five predefined entities, exactly once.

    ``html.unescape`` deliberately is not used: it accepts a much wider HTML
    entity vocabulary and could silently rewrite source code.  ``&amp;`` is
    decoded last so an input such as ``&amp;lt;`` remains the literal ``&lt;``
    rather than being recursively decoded to ``<``.
    """
    return (
        text.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&apos;", "'")
        .replace("&amp;", "&")
    )


def _attrs(text: str) -> dict[str, str]:
    """Parse quoted attributes and decode XML entities in their values."""
    return {m.group(1): _xml_unescape(m.group(3)) for m in _ATTR_RE.finditer(text)}


def _find_tag_end(text: str, start: int) -> int:
    """Return the ``>`` ending the tag at ``start``, respecting quoted attrs."""
    quote: str | None = None
    for index in range(start + 1, len(text)):
        char = text[index]
        if quote is None:
            if char in ('"', "'"):
                quote = char
            elif char == ">":
                return index
        elif char == quote:
            quote = None
    return -1


def _opening_tag(text: str, start: int) -> tuple[str, dict[str, str], bool, int] | None:
    """Parse one opening tag as ``(name, attrs, self_closing, end_index)``."""
    if start < 0 or start >= len(text) or text[start] != "<":
        return None
    end = _find_tag_end(text, start)
    if end < 0:
        return None
    raw = text[start + 1:end]
    if raw.lstrip().startswith("/"):
        return None
    match = _NAME_RE.match(raw.lstrip())
    if match is None:
        return None
    name = match.group(1)
    self_closing = raw.rstrip().endswith("/")
    attr_text = raw.lstrip()[match.end():]
    if self_closing:
        attr_text = attr_text.rstrip()[:-1]
    return name, _attrs(attr_text), self_closing, end


def _body(text: str, name: str, open_end: int) -> tuple[str, int, bool] | None:
    """Return ``(body, next_index, ambiguous_close)`` for a block tag.

    A second closing tag before another opening tag of the same name is the
    legacy silent-truncation shape (``<edit>foo</edit>bar</edit>``).  Surface it
    as an invalid action instead of writing only the prefix.
    """
    close = f"</{name}>"
    close_start = text.find(close, open_end + 1)
    if close_start < 0:
        return None
    next_index = close_start + len(close)
    next_close = text.find(close, next_index)
    next_open_match = re.search(rf"<{re.escape(name)}(?:\s|>)", text[next_index:])
    next_open = next_index + next_open_match.start() if next_open_match else -1
    ambiguous = next_close >= 0 and (next_open < 0 or next_close < next_open)
    if ambiguous:
        next_index = next_close + len(close)
    return text[open_end + 1:close_start], next_index, ambiguous


def _decode_body(body: str, attrs: dict[str, str]) -> str:
    return _xml_unescape(body) if attrs.get("escape", "").lower() == "xml" else body


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

    actions: list[Action] = []
    thinking: list[str] = []
    cursor = 0
    while cursor < len(text):
        start = text.find("<", cursor)
        if start < 0:
            break
        opened = _opening_tag(text, start)
        if opened is None:
            cursor = start + 1
            continue
        kind, attrs, self_closing, open_end = opened

        if self_closing and kind in _SELF_CLOSING_KINDS:
            if kind == "read":
                if attrs.get("path"):
                    actions.append(Action(kind="read", path=attrs["path"]))
            elif kind == "list":
                actions.append(Action(kind="list", path=attrs.get("path", ".")))
            elif kind == "grep":
                if attrs.get("pattern") and attrs.get("path"):
                    actions.append(Action(kind="grep", pattern=attrs["pattern"], path=attrs["path"]))
            elif kind == "glob":
                if attrs.get("pattern"):
                    actions.append(Action(kind="glob", pattern=attrs["pattern"], path=attrs.get("path", ".")))
            elif kind == "webfetch":
                if attrs.get("url"):
                    actions.append(Action(kind="webfetch", url=attrs["url"]))
            elif kind == "mkdir" and attrs.get("path"):
                actions.append(Action(kind="mkdir", path=attrs["path"]))
            cursor = open_end + 1
            continue

        if not self_closing and kind in _BLOCK_KINDS:
            found = _body(text, kind, open_end)
            if found is None:
                cursor = open_end + 1
                continue
            raw_body, cursor, ambiguous = found
            if ambiguous:
                continue
            content = _decode_body(raw_body, attrs)
            if kind == "thinking":
                thinking.append(content.strip())
            elif kind == "plan":
                steps = [_xml_unescape(m.group(1)).strip() for m in _STEP_RE.finditer(content)]
                actions.append(Action(kind="plan", steps=[step for step in steps if step]))
            elif kind in ("edit", "write"):
                if attrs.get("path"):
                    actions.append(Action(
                        kind=kind,
                        path=attrs["path"],
                        content=_strip_block_newlines(content),
                        eol=attrs.get("eol"),
                    ))
            elif kind == "tool":
                name = attrs.get("name")
                try:
                    arguments = json.loads(content)
                except (TypeError, ValueError, json.JSONDecodeError):
                    arguments = None
                if name and isinstance(arguments, dict):
                    actions.append(Action(kind="tool", tool_name=name, arguments=arguments))
            elif kind == "apply_patch":
                actions.append(Action(kind=kind, content=_strip_block_newlines(content), eol=attrs.get("eol")))
            elif kind == "bash":
                actions.append(Action(kind=kind, content=content.strip()))
            elif kind == "question":
                actions.append(Action(
                    kind="question",
                    content=content.strip(),
                    question_class=attrs.get("class") or attrs.get("qclass"),
                ))
            else:
                actions.append(Action(kind=kind, content=content.strip()))
            continue

        cursor = open_end + 1

    return ParseResult(actions=actions, thinking=thinking)


def _named_contents(name: str, text: str) -> list[str]:
    """Extract balanced ``name`` blocks with the same escaping rules as actions."""
    values: list[str] = []
    cursor = 0
    source = text or ""
    while cursor < len(source):
        start = source.find("<", cursor)
        if start < 0:
            break
        opened = _opening_tag(source, start)
        if opened is None:
            cursor = start + 1
            continue
        found_name, attrs, self_closing, open_end = opened
        if found_name != name or self_closing:
            cursor = open_end + 1
            continue
        found = _body(source, name, open_end)
        if found is None:
            cursor = open_end + 1
            continue
        body, cursor, ambiguous = found
        if not ambiguous:
            values.append(_decode_body(body, attrs).strip())
    return values


def tag_content(name: str, text: str) -> str | None:
    """Extract the first ``<name>...</name>`` tag's stripped content, or ``None``.

    A shared helper for modules that need to pull a single tag's text (e.g. the
    brain's ``<verdict>``, ``<reaction>``, ``<scope>``). Compiled regexes are
    cached per tag name for reuse.
    """
    values = _named_contents(name, text)
    return values[0] if values else None


def tag_contents(name: str, text: str) -> list[str]:
    """Extract all ``<name>...</name>`` tags' stripped, non-empty contents.

    A shared helper for modules that need multiple tags (e.g. the brain's
    ``<ask>`` / ``<assume>`` / ``<record>`` tags).
    """
    return [value for value in _named_contents(name, text) if value]
