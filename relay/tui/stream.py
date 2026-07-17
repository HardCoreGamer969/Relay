"""Stream view helpers: caps, scroll-pin, markdown, diffs, folds (U1/U3)."""

from __future__ import annotations

import re

from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.text import Text

from .theme import C_DIM, C_MUTED, W_TEXT, W_TEXT_DIM, W_WARN

# Hard caps for long runs (mirrors + mounted row widgets).
STREAM_MAX_LINES = 500
# Conversation / activity buffers feed ``/log``; keep more than the visible stream.
STREAM_BUFFER_MAX = 2000

_DIFF_HINT = re.compile(
    r"^(diff --git|--- |\+\+\+ |@@ |[+-](?![+-]).)", re.MULTILINE
)
_CODE_FENCE = re.compile(r"```")


def trim_deque_list(items: list, max_len: int) -> None:
    """Drop oldest entries in-place so ``len(items) <= max_len``."""
    if max_len <= 0 or len(items) <= max_len:
        return
    del items[: len(items) - max_len]


def stream_should_follow(stream) -> bool:
    """True when new mounts should pin to the live edge (user was already there)."""
    if stream is None:
        return False
    try:
        if getattr(stream, "is_vertical_scroll_end", None) is not None:
            return bool(stream.is_vertical_scroll_end)
    except Exception:  # noqa: BLE001
        return False
    return True


def trim_stream_children(stream, *, keep=None, max_lines: int = STREAM_MAX_LINES) -> None:
    """Remove oldest non-``keep`` children until the stream is within ``max_lines``."""
    if stream is None or max_lines <= 0:
        return
    keep_set = {keep} if keep is not None else set()
    try:
        children = list(stream.children)
    except Exception:  # noqa: BLE001
        return
    while len(children) > max_lines:
        victim = None
        for child in children:
            if child not in keep_set:
                victim = child
                break
        if victim is None:
            break
        try:
            victim.remove()
        except Exception:  # noqa: BLE001
            pass
        children = [c for c in children if c is not victim]


def looks_like_markdown(text: str) -> bool:
    """Heuristic: fences, headings, or list markers suggest Markdown rendering."""
    if not text or len(text) < 8:
        return False
    if _CODE_FENCE.search(text):
        return True
    lines = text.splitlines()
    mdish = 0
    for line in lines[:40]:
        s = line.lstrip()
        if s.startswith(("# ", "## ", "- ", "* ", "> ")) or (
            len(s) > 2 and s[0].isdigit() and s[1:3] in (". ", ") ")
        ):
            mdish += 1
    return mdish >= 2


def looks_like_diff(text: str) -> bool:
    if not text:
        return False
    hits = _DIFF_HINT.findall(text)
    return len(hits) >= 2


def render_conversation_body(text: str):
    """Return a Rich renderable for a brain/user body (Markdown when structured)."""
    if looks_like_markdown(text):
        try:
            return Markdown(text)
        except Exception:  # noqa: BLE001 -- fall back to plain
            pass
    return text


def render_observation(text: str, *, expanded: bool = False, max_preview: int = 60):
    """Render a tool observation: diff Syntax when diff-shaped, else fold/preview."""
    raw = text or ""
    if looks_like_diff(raw):
        body = raw if expanded else "\n".join(raw.splitlines()[:12])
        try:
            return Syntax(body, "diff", theme="monokai", word_wrap=False, line_numbers=False)
        except Exception:  # noqa: BLE001
            pass
    if expanded or len(raw) <= max_preview:
        return raw
    return raw[:max_preview] + "…"


def tool_summary_line(label: str, result: str = "", *, folded: bool = True) -> Text:
    """Compact tool call line with fold marker."""
    text = Text()
    text.append("  ▸ ", style=W_TEXT_DIM)
    text.append(label, style=C_MUTED)
    if result:
        preview = result if not folded or len(result) <= 60 else result[:60] + "…"
        text.append(f"  · {preview}", style=C_DIM)
    if folded and result and len(result) > 60:
        text.append("  [+]", style=W_WARN)
    elif result and len(result) > 60:
        text.append("  [-]", style=C_DIM)
    return text


def find_in_lines(lines: list[str], query: str) -> list[int]:
    """Return indices of lines containing ``query`` (case-insensitive)."""
    q = (query or "").strip().lower()
    if not q:
        return []
    return [i for i, line in enumerate(lines) if q in line.lower()]
