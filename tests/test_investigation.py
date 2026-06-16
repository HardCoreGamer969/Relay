"""Network-free tests for the generalized brain-investigation primitive.

The primitive is driven in isolation by a fake brain callable (passed as
``model_call``), so no network and no real model are involved. These cover the
properties the primitive OWNS for every consumer: it reads, it bounds, it falls
back to a safe default, it is strictly read-only, and parse failures cannot spin.
"""

from __future__ import annotations

from types import SimpleNamespace

from relay.investigation import investigate
from relay.loop import MAX_CONSECUTIVE_PARSE_FAILURES
from relay.protocol import parse
from relay.tools import Tools


class FakeBrain:
    """A test brain callable: returns queued replies, then repeats the last forever.

    Repeating the last reply lets a single fixed reply (e.g. an always-read or an
    always-garbage turn) drive the never-terminating / parse-failure cases.
    """

    def __init__(self, *replies):
        self.queue = list(replies)
        self.last = replies[-1] if replies else ""
        self.calls = 0

    def __call__(self, role, messages, **kwargs):
        self.calls += 1
        if self.queue:
            self.last = self.queue.pop(0)
        return SimpleNamespace(text=self.last, record=None)


def _PARSED(reply):
    return ("PARSED", reply)


def _SAFE():
    return ("SAFE", None)


def test_immediate_terminator_is_one_call(tmp_path):
    brain = FakeBrain("<verdict>accept</verdict>")
    result = investigate(
        "sys", "seed", terminators=("verdict",), parse_terminal=_PARSED,
        safe_default=_SAFE, budget=4, tools=Tools(tmp_path), model_call=brain,
    )
    assert result == ("PARSED", "<verdict>accept</verdict>")
    assert brain.calls == 1  # terminator on the first reply -> exactly one brain call


def test_reads_then_returns_parsed_terminal(tmp_path):
    (tmp_path / "x.txt").write_text("REAL FILE BODY", encoding="utf-8")
    brain = FakeBrain('<read path="x.txt"/>', "<verdict>accept</verdict>")
    events = []
    result = investigate(
        "sys", "seed", terminators=("verdict",), parse_terminal=_PARSED,
        safe_default=_SAFE, budget=4, tools=Tools(tmp_path), model_call=brain,
        emit=lambda k, m, p: events.append((k, m, p)),
    )
    assert result == ("PARSED", "<verdict>accept</verdict>")
    assert brain.calls == 2  # one read turn, then the verdict turn
    # The read actually ran against the real file and its body reached the brain.
    read_events = [p for k, m, p in events if k == "brain_action"]
    assert any("REAL FILE BODY" in p["observation"] for p in read_events)


def test_budget_bounds_and_returns_safe_default(tmp_path):
    (tmp_path / "x.txt").write_text("body", encoding="utf-8")
    brain = FakeBrain('<read path="x.txt"/>')  # always reads, never terminates
    result = investigate(
        "sys", "seed", terminators=("verdict",), parse_terminal=_PARSED,
        safe_default=_SAFE, budget=3, tools=Tools(tmp_path), model_call=brain,
    )
    assert result == ("SAFE", None)   # exhausted -> the consumer's safe default
    assert brain.calls == 3           # exactly the budget, not one more


def test_write_actions_are_refused_never_executed(tmp_path):
    # The read-only-brain property: a model that emits edit/bash gets refused and the
    # filesystem is untouched -- the brain investigation loop can NEVER write.
    brain = FakeBrain('<edit path="evil.txt">PWNED</edit>\n<bash>echo hi > created.txt</bash>')
    result = investigate(
        "sys", "seed", terminators=("verdict",), parse_terminal=_PARSED,
        safe_default=_SAFE, budget=3, tools=Tools(tmp_path), model_call=brain,
    )
    assert result == ("SAFE", None)
    assert not (tmp_path / "evil.txt").exists()     # edit refused, file never written
    assert not (tmp_path / "created.txt").exists()  # bash refused, command never ran
    assert brain.calls == 3                          # bounded (it never terminates)


def test_parse_failures_are_bounded(tmp_path):
    brain = FakeBrain("just chatting, no tags at all")  # no terminator, no action
    result = investigate(
        "sys", "seed", terminators=("verdict",), parse_terminal=_PARSED,
        safe_default=_SAFE, budget=10, tools=Tools(tmp_path), model_call=brain,
    )
    assert result == ("SAFE", None)
    # Bounded by the consecutive-parse-failure limit (< budget), so it cannot spin.
    assert brain.calls == MAX_CONSECUTIVE_PARSE_FAILURES


def test_is_terminal_refinement_fits_plan_consumers(tmp_path):
    # Proves the replan/evolve adapter fit: their terminal condition is richer than
    # tag-presence -- an empty <plan></plan> must keep investigating, a non-empty
    # <plan> (or <abort>) terminates. The optional is_terminal hook expresses that.
    def nonempty_plan_or_abort(reply):
        p = parse(reply)
        if p.first("abort") is not None:
            return True
        plan = p.first("plan")
        return bool(plan and plan.steps)

    brain = FakeBrain("<plan></plan>", "<plan><step>do A</step></plan>")
    result = investigate(
        "sys", "seed", terminators=("plan", "abort"), is_terminal=nonempty_plan_or_abort,
        parse_terminal=lambda r: parse(r).first("plan").steps, safe_default=lambda: None,
        budget=4, tools=Tools(tmp_path), model_call=brain,
    )
    assert result == ["do A"]
    assert brain.calls == 2  # the empty plan did NOT terminate; the non-empty one did


def test_no_tools_handle_degrades_gracefully(tmp_path):
    # With no filesystem handle, a read cannot run but the loop stays bounded and
    # returns the safe default (it never crashes on a missing Tools).
    brain = FakeBrain('<read path="x.txt"/>')
    result = investigate(
        "sys", "seed", terminators=("verdict",), parse_terminal=_PARSED,
        safe_default=_SAFE, budget=2, tools=None, model_call=brain,
    )
    assert result == ("SAFE", None)
    assert brain.calls == 2
