"""Stream view helpers: caps, scroll-pin, and bounded mirrors (U1).

The live plan block still mounts as a ``Static`` inside ``#stream`` (U2 moves
it to the plan dock). Regular rows stay ``Static`` children for now; this module
bounds memory and avoids yanking scrollback while the user is reading.
"""

from __future__ import annotations

# Hard caps for long runs (mirrors + mounted row widgets).
STREAM_MAX_LINES = 500
# Conversation / activity buffers feed ``/log``; keep more than the visible stream.
STREAM_BUFFER_MAX = 2000


def trim_deque_list(items: list, max_len: int) -> None:
    """Drop oldest entries in-place so ``len(items) <= max_len``."""
    if max_len <= 0 or len(items) <= max_len:
        return
    del items[: len(items) - max_len]


def stream_should_follow(stream) -> bool:
    """True when new mounts should pin to the live edge (user was already there).

    Empty / unscrollable streams always follow. If the reader scrolled up, leave
    them alone so mid-run scrollback stays readable.
    """
    if stream is None:
        return False
    try:
        # Textual: True when the vertical scrollbar is at (or past) the end.
        if getattr(stream, "is_vertical_scroll_end", None) is not None:
            return bool(stream.is_vertical_scroll_end)
    except Exception:  # noqa: BLE001 -- widget mid-teardown
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
        except Exception:  # noqa: BLE001 -- already gone
            pass
        children = [c for c in children if c is not victim]
