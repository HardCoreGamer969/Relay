"""Network-free tests for plan memory (relay/memory.py).

The summarizer (`call_model`) is monkeypatched where compaction would call it.
"""

from __future__ import annotations

from types import SimpleNamespace

import relay.memory as memory_mod
from relay.memory import (
    MemoryEntry,
    PlanMemory,
    entry_tokens,
    estimate_tokens,
    memory_budget,
    small_window_warning,
)


def _fake_result(text):
    return SimpleNamespace(text=text, record=None)


# --- entry + store ----------------------------------------------------------


def test_entry_carries_provenance_and_dual_forms():
    mem = PlanMemory()
    e = mem.remember(
        "fact", "the API base url is https://x/api", "found the API base url",
        provenance="step3", tags=["api"],
    )
    assert e.kind == "fact"
    assert e.provenance == "step3"
    assert e.detail != e.summary  # precise vs human-facing
    assert e.tags == ["api"]


def test_add_appends_with_stable_monotonic_order():
    mem = PlanMemory()
    a = mem.remember("fact", "a", "a")
    b = mem.remember("decision", "b", "b")
    c = mem.remember("fact", "c", "c")
    assert [x.created_at for x in mem.entries] == [0, 1, 2]
    assert (a.id, b.id, c.id) == ("m0", "m1", "m2")
    assert [x.id for x in mem.entries] == ["m0", "m1", "m2"]


def test_snapshot_round_trips_equal_and_continues_ordinals():
    mem = PlanMemory()
    mem.remember("fact", "d1", "s1", provenance="p", tags=["t"])
    mem.remember("decision", "d2", "s2")

    restored = PlanMemory.from_state(mem.to_state())
    assert restored == mem

    # Adding after restore keeps the monotonic ordinal sequence.
    e = restored.remember("fact", "d3", "s3")
    assert e.created_at == 2


# --- budget slicing: the headline behavior at both window extremes ----------


def test_relevant_large_budget_returns_all():
    mem = PlanMemory()
    for i in range(12):
        mem.remember("fact", f"detail number {i} about alpha subsystem", f"s{i}", tags=["alpha"])
    got = mem.relevant("alpha subsystem", budget_tokens=100_000)
    assert len(got) == 12  # a 200K-style budget: everything fits


def test_relevant_tiny_budget_slices_and_stays_under_cap():
    mem = PlanMemory()
    for i in range(20):
        mem.remember("fact", "alpha " + ("x" * 200), f"s{i}", tags=["alpha"])
    budget = 120  # an 8K-window-derived tight budget
    got = mem.relevant("alpha", budget_tokens=budget)

    assert 0 < len(got) < 20  # only the few highest-ranked that fit
    assert sum(entry_tokens(e) for e in got) <= budget  # provably under the cap


def test_relevant_zero_budget_returns_nothing():
    mem = PlanMemory()
    mem.remember("fact", "anything", "s")
    assert mem.relevant("anything", budget_tokens=0) == []


def test_relevant_ranks_keyword_overlap_above_unrelated():
    mem = PlanMemory()
    mem.remember("fact", "totally unrelated note about zebras", "z", tags=["zoo"])
    mem.remember("fact", "the widget endpoint lives at /api/widget", "w", tags=["widget"])
    got = mem.relevant("widget endpoint", budget_tokens=40)  # room for ~one entry
    assert got and "widget" in got[0].detail


# --- compress, don't truncate ----------------------------------------------


def test_compacted_context_compresses_overflow(monkeypatch):
    calls = []

    def fake_call(role, messages, **kwargs):
        calls.append((role, messages, kwargs))
        return _fake_result("DENSE-SUMMARY of older alpha facts")

    monkeypatch.setattr(memory_mod, "call_model", fake_call)

    mem = PlanMemory()
    for i in range(12):
        mem.remember("fact", f"alpha fact {i} " + ("y" * 60), f"s{i}", tags=["alpha"])

    budget = 140
    out = mem.compacted_context("alpha", budget_tokens=budget, client=object(), models=None)

    assert "DENSE-SUMMARY" in out  # overflow knowledge survives as a summary
    assert estimate_tokens(out) <= budget  # fits the budget
    assert len(calls) == 1 and calls[0][0] == "brain"  # one brain summary call


def test_compacted_context_no_overflow_makes_no_summary_call(monkeypatch):
    calls = []
    monkeypatch.setattr(
        memory_mod, "call_model",
        lambda *a, **k: calls.append(1) or _fake_result("unused"),
    )
    mem = PlanMemory()
    mem.remember("fact", "small one", "s1", tags=["alpha"])
    mem.remember("fact", "small two", "s2", tags=["alpha"])

    out = mem.compacted_context("alpha", budget_tokens=4000, client=object(), models=None)
    assert calls == []  # everything fit verbatim; no summarization
    assert "small one" in out and "small two" in out


def test_compacted_context_summarizer_failure_degrades(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("model down")

    monkeypatch.setattr(memory_mod, "call_model", boom)

    mem = PlanMemory()
    for i in range(12):
        mem.remember("fact", f"alpha fact {i} " + ("y" * 60), f"s{i}", tags=["alpha"])

    budget = 140
    out = mem.compacted_context("alpha", budget_tokens=budget, client=object(), models=None)
    # No crash; a visible note about the loss; still within budget.
    assert ("omitted" in out) or ("unavailable" in out)
    assert estimate_tokens(out) <= budget


# --- small-window warning ---------------------------------------------------


def test_small_window_warning_fires_when_too_small():
    mem = PlanMemory()
    for i in range(5):
        mem.remember("fact", "z" * 400, "s")
    warn = small_window_warning(mem, 1000)
    assert warn is not None and "RELAY_BRAIN_CONTEXT" in warn


def test_small_window_warning_silent_when_it_fits():
    mem = PlanMemory()
    for i in range(5):
        mem.remember("fact", "z" * 400, "s")
    assert small_window_warning(mem, 200_000) is None


def test_small_window_warning_silent_on_empty_memory():
    assert small_window_warning(PlanMemory(), 100) is None


# --- budget math ------------------------------------------------------------


def test_memory_budget_scales_with_window():
    assert memory_budget(200_000) > memory_budget(8192)
    assert memory_budget(8192) == int(8192 * 0.5) - 1024
    assert memory_budget(100) == 0  # tiny window -> no budget after reserve
