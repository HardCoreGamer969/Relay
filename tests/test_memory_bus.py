"""Network-free tests for the three-pool MemoryBus (relay/memory.py, v0.0.29).

The bus composes three UNMODIFIED PlanMemory sub-stores; these tests pin the
Stage-1 contract: write-routing, the brain ∪ shared read, the hands-shared-only
read, the mechanized union cap, per-pool counters, within-pool dedup, the
configured budgets, and the nested snapshot shape. The summarizer (`call_model`)
is monkeypatched wherever a tiny budget would force compaction.
"""

from __future__ import annotations

from types import SimpleNamespace

import relay.memory as memory_mod
from relay.memory import (
    MemoryBus,
    PlanMemory,
    POOL_BRAIN,
    POOL_HANDS,
    POOL_SHARED,
    SHARED_READ_FRACTION,
    estimate_tokens,
    memory_budget,
)


def _fake_result(text):
    return SimpleNamespace(text=text, record=None)


# --- write routing ----------------------------------------------------------


def test_remember_defaults_to_the_brain_pool():
    bus = MemoryBus()
    bus.remember("fact", "brain detail", "brain")
    assert len(bus.brain.entries) == 1
    assert not bus.hands.entries
    assert not bus.shared.entries


def test_remember_routes_to_the_named_pool():
    bus = MemoryBus()
    bus.remember("fact", "b", "b", pool=POOL_BRAIN)
    bus.remember("fact", "h", "h", pool=POOL_HANDS)
    bus.remember("confirmation", "s", "s", pool=POOL_SHARED)
    assert [e.detail for e in bus.brain.entries] == ["b"]
    assert [e.detail for e in bus.hands.entries] == ["h"]
    assert [e.detail for e in bus.shared.entries] == ["s"]


def test_pool_rejects_unknown_name():
    bus = MemoryBus()
    try:
        bus.pool("nope")
    except KeyError:
        return
    raise AssertionError("expected KeyError for an unknown pool")


def test_entries_is_the_union_view():
    bus = MemoryBus()
    bus.remember("fact", "b", "b", pool=POOL_BRAIN)
    bus.remember("fact", "h", "h", pool=POOL_HANDS)
    bus.remember("fact", "s", "s", pool=POOL_SHARED)
    details = [e.detail for e in bus.entries]
    assert details == ["b", "h", "s"]  # brain, then hands, then shared


# --- per-pool counters + within-pool dedup ----------------------------------


def test_counters_are_independent_per_pool():
    bus = MemoryBus()
    a = bus.remember("fact", "alpha one", "a", pool=POOL_BRAIN)
    b = bus.remember("fact", "beta two", "b", pool=POOL_BRAIN)
    s = bus.remember("fact", "gamma three", "s", pool=POOL_SHARED)
    # Each sub-store keeps its OWN monotonic counter (no global ordinal space).
    assert (a.id, b.id) == ("m0", "m1")
    assert s.id == "m0"  # shared pool's own counter, independent of the brain's


def test_dedup_is_within_pool_only():
    bus = MemoryBus()
    original = "the main function lacks the required input loop implementation"
    restated = "the main function does not contain the required input loop implementation"
    first = bus.remember("fact", original, "no loop", pool=POOL_BRAIN)
    # A near-restatement in the SAME pool is suppressed (within-pool dedup).
    dup = bus.remember("fact", restated, "no loop", pool=POOL_BRAIN)
    assert first is not None and dup is None
    # The SAME restatement in a DIFFERENT pool is kept (no cross-pool dedup, Stage 1).
    other = bus.remember("fact", restated, "no loop", pool=POOL_SHARED)
    assert other is not None
    assert len(bus.brain.entries) == 1 and len(bus.shared.entries) == 1


# --- budgeting --------------------------------------------------------------


def test_configure_budgets_sizes_each_pool_to_its_window():
    bus = MemoryBus().configure_budgets(brain_window=200_000, hands_window=8_192)
    assert bus.brain_budget == memory_budget(200_000)
    assert bus.hands_budget == memory_budget(8_192)
    # Shared must fit the SMALLER window (it renders into both contexts).
    assert bus.shared_budget == memory_budget(min(200_000, 8_192))
    assert bus.shared_budget == memory_budget(8_192)


def test_configure_budgets_shared_uses_min_when_hands_is_larger():
    bus = MemoryBus().configure_budgets(brain_window=16_000, hands_window=128_000)
    assert bus.shared_budget == memory_budget(16_000)


# --- reads: brain ∪ shared, hands shared-only -------------------------------


def test_brain_context_reads_brain_union_shared_not_hands():
    bus = MemoryBus().configure_budgets(brain_window=200_000, hands_window=200_000)
    bus.remember("decision", "BRAINFACT chose sqlite", "brain", pool=POOL_BRAIN)
    bus.remember("confirmation", "SHAREDDIRECTIVE use oauth", "shared", pool=POOL_SHARED)
    bus.remember("fact", "HANDSSCRATCH tried a thing", "hands", pool=POOL_HANDS)
    out = bus.brain_context("chose oauth thing", bus.brain_budget)
    assert "BRAINFACT" in out  # brain pool: visible to the brain
    assert "SHAREDDIRECTIVE" in out  # shared pool: visible to the brain
    assert "HANDSSCRATCH" not in out  # hands pool: NEVER visible to the brain


def test_hands_context_reads_shared_only():
    bus = MemoryBus().configure_budgets(brain_window=200_000, hands_window=200_000)
    bus.remember("decision", "BRAINFACT chose sqlite", "brain", pool=POOL_BRAIN)
    bus.remember("confirmation", "SHAREDDIRECTIVE use oauth", "shared", pool=POOL_SHARED)
    bus.remember("fact", "HANDSSCRATCH tried a thing", "hands", pool=POOL_HANDS)
    out = bus.hands_context("oauth directive", bus.hands_budget)
    assert "SHAREDDIRECTIVE" in out  # shared: the hands sees the brain's directives
    assert "BRAINFACT" not in out  # brain pool: NEVER leaks to the hands
    assert "HANDSSCRATCH" not in out  # Stage 1: the hands does NOT read its own pool yet


def test_hands_context_empty_when_shared_empty():
    bus = MemoryBus().configure_budgets(brain_window=200_000, hands_window=200_000)
    bus.remember("fact", "brain only", "b", pool=POOL_BRAIN)
    assert bus.hands_context("anything", bus.hands_budget) == ""


# --- the mechanized union cap -----------------------------------------------


def test_brain_union_stays_within_the_brain_budget(monkeypatch):
    # Force overflow in BOTH pools (tiny budget, many entries) so compaction runs;
    # the union of the two compacted slices must still fit the brain budget.
    monkeypatch.setattr(memory_mod, "call_model", lambda *a, **k: _fake_result("compacted note"))
    bus = MemoryBus().configure_budgets(brain_window=4_096, hands_window=4_096)
    for i in range(40):
        bus.remember("fact", f"brain entry number {i} with assorted words here", f"b{i}", pool=POOL_BRAIN)
        bus.remember("fact", f"shared entry number {i} with assorted words here", f"s{i}", pool=POOL_SHARED)
    out = bus.brain_context("entry words", bus.brain_budget)
    assert estimate_tokens(out) <= bus.brain_budget
    # And the shared portion is bounded by the reserved fraction (the cap mechanism).
    assert bus._shared_read_cap(bus.brain_budget) <= int(bus.brain_budget * SHARED_READ_FRACTION) + 1


def test_brain_context_zero_budget_is_empty():
    bus = MemoryBus()
    bus.remember("fact", "x", "x", pool=POOL_BRAIN)
    assert bus.brain_context("x", 0) == ""


# --- snapshot shape ---------------------------------------------------------


def test_to_from_state_round_trips_three_pools():
    bus = MemoryBus()
    bus.remember("fact", "b", "b", pool=POOL_BRAIN)
    bus.remember("fact", "h", "h", pool=POOL_HANDS)
    bus.remember("confirmation", "s", "s", pool=POOL_SHARED)
    restored = MemoryBus.from_state(bus.to_state())
    assert [e.detail for e in restored.brain.entries] == ["b"]
    assert [e.detail for e in restored.hands.entries] == ["h"]
    assert [e.detail for e in restored.shared.entries] == ["s"]


def test_from_state_accepts_a_legacy_single_pool_snapshot():
    legacy = PlanMemory()
    legacy.remember("fact", "old detail", "old")
    # A v0.06 single-pool snapshot (no "pools" key) loads into the brain pool.
    restored = MemoryBus.from_state(legacy.to_state())
    assert [e.detail for e in restored.brain.entries] == ["old detail"]
    assert not restored.hands.entries and not restored.shared.entries
