"""Durable project memory: persist the SHARED pool across runs.

Repo-local store at ``<root>/.relay/memory.json``. Only the shared pool is
written — brain/hands private pools never leave the process. Entries may be
``pinned`` (tag) so budget trim prefers keeping them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from relay.memory import (
    POOL_SHARED,
    MemoryBus,
    MemoryEntry,
    PlanMemory,
)

SCHEMA_VERSION = 1
DEFAULT_MAX_ENTRIES = 80
PIN_TAG = "pinned"


def memory_path(root: str | Path) -> Path:
    return Path(root) / ".relay" / "memory.json"


def load_shared(root: str | Path) -> PlanMemory:
    """Load the durable shared pool, or an empty :class:`PlanMemory`."""
    path = memory_path(root)
    if not path.exists():
        return PlanMemory()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return PlanMemory()
    if not isinstance(data, dict):
        return PlanMemory()
    shared = data.get("shared") or data.get("pools", {}).get(POOL_SHARED) or {}
    if not isinstance(shared, dict):
        return PlanMemory()
    try:
        return PlanMemory.from_state(shared)
    except (TypeError, KeyError, ValueError):
        return PlanMemory()


def save_shared(
    root: str | Path,
    shared: PlanMemory,
    *,
    max_entries: int = DEFAULT_MAX_ENTRIES,
) -> Path:
    """Persist ``shared``, trimming oldest unpinned entries if over cap."""
    trimmed = _trim(shared, max_entries=max_entries)
    path = memory_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "shared": trimmed.to_state(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    return path


def merge_shared_into_bus(bus: MemoryBus, root: str | Path) -> int:
    """Load durable shared entries into ``bus.shared`` (skip near-duplicates).

    Returns the number of entries newly added.
    """
    durable = load_shared(root)
    added = 0
    for entry in durable.entries:
        stored = bus.shared.remember(
            entry.kind,
            entry.detail,
            entry.summary,
            provenance=entry.provenance or "durable",
            tags=list(entry.tags),
        )
        if stored is not None:
            added += 1
    return added


def capture_bus_shared(bus: MemoryBus, root: str | Path) -> Path:
    """Write the bus's current shared pool to disk (after a run)."""
    return save_shared(root, bus.shared)


def list_entries(root: str | Path) -> list[MemoryEntry]:
    return list(load_shared(root).entries)


def pin_entry(root: str | Path, entry_id: str) -> bool:
    mem = load_shared(root)
    updated = False
    new_entries: list[MemoryEntry] = []
    for entry in mem.entries:
        if entry.id == entry_id:
            tags = list(entry.tags)
            if PIN_TAG not in tags:
                tags.append(PIN_TAG)
                entry = MemoryEntry(
                    id=entry.id,
                    kind=entry.kind,
                    provenance=entry.provenance,
                    detail=entry.detail,
                    summary=entry.summary,
                    created_at=entry.created_at,
                    tags=tags,
                )
                updated = True
        new_entries.append(entry)
    if not updated and not any(e.id == entry_id for e in mem.entries):
        return False
    mem.entries = new_entries
    save_shared(root, mem)
    return True


def forget_entry(root: str | Path, entry_id: str) -> bool:
    mem = load_shared(root)
    before = len(mem.entries)
    mem.entries = [e for e in mem.entries if e.id != entry_id]
    if len(mem.entries) == before:
        return False
    save_shared(root, mem)
    return True


def _trim(shared: PlanMemory, *, max_entries: int) -> PlanMemory:
    if max_entries <= 0 or len(shared.entries) <= max_entries:
        return shared
    pinned = [e for e in shared.entries if PIN_TAG in e.tags]
    unpinned = [e for e in shared.entries if PIN_TAG not in e.tags]
    # Keep newest unpinned.
    unpinned.sort(key=lambda e: e.created_at, reverse=True)
    keep_unpinned = max(0, max_entries - len(pinned))
    kept = pinned + unpinned[:keep_unpinned]
    kept.sort(key=lambda e: e.created_at)
    return PlanMemory(entries=kept, counter=shared.counter)
