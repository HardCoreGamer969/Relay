"""v0.0.28 Part 3: the input queue + /queue + /redirect + up-arrow recall.

steer = apply now (replan remainder); queue = hold the input, finish the current
step, then consume next (FIFO). up-arrow is ONE unified recall-and-edit affordance
across goals, steers, and queued items. These pin the queue/history mechanism
headlessly (no Textual) plus the parse helper. Network-free.
"""

from __future__ import annotations

from relay.bridge import InputHistory, InputQueue, Session
from relay.tui import _parse_inline_command


# --- InputQueue: ordered FIFO -----------------------------------------------


def test_queue_is_fifo():
    q = InputQueue()
    assert q.dequeue() is None and not q
    q.enqueue("first")
    q.enqueue("second")
    q.enqueue("third")
    assert len(q) == 3
    assert q.pending() == ["first", "second", "third"]
    assert q.dequeue() == "first"
    assert q.dequeue() == "second"
    assert q.dequeue() == "third"
    assert q.dequeue() is None  # drained, deterministic


# --- InputHistory: one unified recall-and-edit cursor -----------------------


def test_history_recall_older_walks_back():
    h = InputHistory()
    h.add("build the app")
    h.add("add tests")
    h.add("write docs")
    assert h.recall_older() == "write docs"   # most recent first
    assert h.recall_older() == "add tests"
    assert h.recall_older() == "build the app"
    assert h.recall_older() == "build the app"  # clamps at the oldest


def test_history_recall_newer_walks_forward_then_clears():
    h = InputHistory()
    h.add("one")
    h.add("two")
    h.recall_older()              # -> "two"
    h.recall_older()              # -> "one"
    assert h.recall_newer() == "two"
    assert h.recall_newer() == ""   # stepped past the newest: a cleared field


def test_history_add_resets_the_cursor():
    h = InputHistory()
    h.add("a")
    h.add("b")
    assert h.recall_older() == "b"
    h.add("c")                      # a fresh submission resets recall to newest
    assert h.recall_older() == "c"


def test_history_includes_queued_items(tmp_path):
    """Up-arrow recalls the most recent input INCLUDING queued items."""
    session = Session(tmp_path)
    session.history.add("first goal")
    session.queue.enqueue("queued task")
    session.history.add("queued task")  # /queue records to history too
    assert session.history.recall_older() == "queued task"


def test_recalled_queued_item_can_be_re_queued(tmp_path):
    """A recalled+edited queued item re-submits correctly (re-queue path)."""
    session = Session(tmp_path)
    session.queue.enqueue("do X")
    session.history.add("do X")
    recalled = session.history.recall_older()
    assert recalled == "do X"
    edited = recalled + " carefully"
    session.queue.enqueue(edited)            # re-submit as another queued item
    assert session.queue.pending() == ["do X", "do X carefully"]


# --- the inline-command parser ----------------------------------------------


def test_parse_inline_command():
    assert _parse_inline_command("/queue do the thing") == ("queue", "do the thing")
    assert _parse_inline_command("/redirect use Postgres") == ("redirect", "use Postgres")
    assert _parse_inline_command("/queue") == ("queue", "")     # no arg
    assert _parse_inline_command("not a command") is None
    assert _parse_inline_command("/clear") == ("clear", "")     # other commands parse too
