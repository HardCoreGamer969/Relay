"""Tests for durable shared memory (A3)."""

from __future__ import annotations

from relay.durable_memory import (
    PIN_TAG,
    capture_bus_shared,
    forget_entry,
    list_entries,
    load_shared,
    merge_shared_into_bus,
    pin_entry,
    save_shared,
)
from relay.memory import MemoryBus, PlanMemory


def test_round_trip_shared_only(tmp_path):
    mem = PlanMemory()
    mem.remember("fact", "auth lives in auth.py", "auth in auth.py", provenance="finding")
    save_shared(tmp_path, mem)
    loaded = load_shared(tmp_path)
    assert len(loaded.entries) == 1
    assert "auth.py" in loaded.entries[0].detail


def test_merge_into_bus_does_not_touch_brain(tmp_path):
    bus = MemoryBus()
    bus.brain.remember("decision", "private brain note", "private", provenance="brain")
    durable = PlanMemory()
    durable.remember("fact", "shared finding", "shared", provenance="run1")
    save_shared(tmp_path, durable)
    added = merge_shared_into_bus(bus, tmp_path)
    assert added == 1
    assert len(bus.brain.entries) == 1
    assert len(bus.shared.entries) == 1
    # Capture must not write brain pool.
    capture_bus_shared(bus, tmp_path)
    again = load_shared(tmp_path)
    assert all("private" not in e.detail for e in again.entries)


def test_pin_and_forget(tmp_path):
    mem = PlanMemory()
    e = mem.remember("fact", "keep me", "keep", provenance="t")
    assert e is not None
    save_shared(tmp_path, mem)
    assert pin_entry(tmp_path, e.id)
    pinned = list_entries(tmp_path)[0]
    assert PIN_TAG in pinned.tags
    assert forget_entry(tmp_path, e.id)
    assert list_entries(tmp_path) == []


def test_trim_prefers_pinned(tmp_path):
    mem = PlanMemory()
    for i in range(5):
        mem.remember("fact", f"note {i}", f"n{i}", provenance="t", tags=[PIN_TAG] if i == 0 else [])
    save_shared(tmp_path, mem, max_entries=2)
    ids = {e.summary for e in list_entries(tmp_path)}
    assert "n0" in ids  # pinned survived
    assert len(ids) == 2
