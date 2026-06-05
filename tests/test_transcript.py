"""Network-free tests for the continuous transcript (relay/transcript.py).

The brain (`call_model`) is patched in the transcript module so the narrative
compaction path is exercised without network. Plan memory is real (in-process).
"""

from __future__ import annotations

from types import SimpleNamespace

import relay.transcript as tr
from relay.memory import PlanMemory, estimate_tokens
from relay.transcript import (
    Transcript,
    Turn,
    compact_transcript,
    record_decision,
    render_for_brain,
)


def _resp(text):
    return SimpleNamespace(text=text, record=None)


class _Brain:
    """Records every (system, user) prompt; returns a fixed reply. No network."""

    def __init__(self, reply="Earlier, you asked for X and the brain proposed Y, then you committed."):
        self.reply = reply
        self.systems = []
        self.calls = 0

    def __call__(self, role, messages, **kwargs):
        self.calls += 1
        self.systems.append(messages[0]["content"])
        return _resp(self.reply)


# --- structure + snapshot ---------------------------------------------------


def test_add_assigns_monotonic_id_and_ordinal():
    t = Transcript()
    a = t.record("user", "planning", "build a website")
    b = t.record("brain", "proposal", "here is the plan")
    assert (a.id, a.created_at) == ("t0", 0)
    assert (b.id, b.created_at) == ("t1", 1)
    assert [x.text for x in t.turns] == ["build a website", "here is the plan"]


def test_recent_returns_chronological_tail():
    t = Transcript()
    for i in range(5):
        t.record("user", "reaction", f"r{i}")
    recent = t.recent(2)
    assert [x.text for x in recent] == ["r3", "r4"]
    assert t.recent(0) == []


def test_snapshot_round_trips_equal():
    t = Transcript()
    t.record("user", "planning", "goal")
    t.record("brain", "proposal", "plan", refs=["step0", "m1"])
    restored = Transcript.from_state(t.to_state())
    assert restored == t
    assert restored.counter == t.counter
    assert restored.turns[1].refs == ["step0", "m1"]


# --- transcript-first, memory-derived ---------------------------------------


def test_record_decision_is_transcript_first_and_memory_derived():
    transcript = Transcript()
    memory = PlanMemory()
    turn, entry = record_decision(
        transcript, memory,
        text="yes, Google sign-in only",
        detail="User decided on 'auth?': Google sign-in only",
        summary="user: Google only",
        kind="confirmation", phase="decision",
    )
    # The authoritative record is the transcript turn...
    assert transcript.turns == [turn]
    assert turn.phase == "decision" and "Google" in turn.text
    # ...and the memory entry is DERIVED from it, linked by provenance.
    assert entry is not None
    assert entry.provenance == f"transcript:{turn.id}"
    assert entry in memory.entries
    assert entry.kind == "confirmation"


def test_record_decision_without_memory_still_records_turn():
    transcript = Transcript()
    turn, entry = record_decision(transcript, None, text="ok", detail="d", summary="s")
    assert entry is None
    assert transcript.turns == [turn]  # transcript is the source of truth regardless


# --- readability-preserving compaction --------------------------------------


def _long_transcript(n=10):
    t = Transcript()
    t.record("user", "planning", "build me a todo app")
    t.record("brain", "proposal", "propose a Flask backend with SQLite")
    t.record("user", "reaction", "change auth to Google-only")
    for i in range(n):
        t.record("brain" if i % 2 else "user", "reaction", f"older turn number {i} with some content")
    t.record("user", "commit", "ok build it")
    t.record("brain", "result", "Run ended: completed. 3/3 steps done.")
    return t


def test_compaction_keeps_recent_verbatim_and_narrates_older(monkeypatch):
    brain = _Brain(reply="Earlier, you asked for a todo app, the brain proposed Flask, you changed auth, then committed.")
    monkeypatch.setattr(tr, "call_model", brain)

    t = _long_transcript()
    compacted = compact_transcript(t, keep_recent=3, client=object())

    # One readable narrative turn for the older block, then the recent turns verbatim.
    assert compacted.turns[0].phase == "summary"
    assert "Earlier" in compacted.turns[0].text  # prose-shaped narrative, not dense notes
    assert [x.text for x in compacted.turns[1:]] == [x.text for x in t.turns[-3:]]
    # The narrative was produced by the READABLE narrative prompt -- NOT memory's
    # dense compaction (compacted_context / its "densest possible note" system).
    joined = " ".join(brain.systems).lower()
    assert "readable narrative" in joined
    assert "densest possible note" not in joined


def test_compaction_noop_when_short(monkeypatch):
    brain = _Brain()
    monkeypatch.setattr(tr, "call_model", brain)
    t = Transcript()
    t.record("user", "planning", "x")
    t.record("brain", "proposal", "y")
    compacted = compact_transcript(t, keep_recent=6, client=object())
    assert compacted == t            # unchanged
    assert brain.calls == 0          # no narration call when nothing to fold


def test_compaction_degrades_gracefully_on_summarizer_failure(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("model down")

    monkeypatch.setattr(tr, "call_model", boom)
    t = _long_transcript()
    compacted = compact_transcript(t, keep_recent=3, client=object())  # must not raise

    # Recent turns survive verbatim; the older block degrades to a noted, readable brief.
    assert [x.text for x in compacted.turns[1:]] == [x.text for x in t.turns[-3:]]
    assert compacted.turns[0].phase == "summary"
    assert "unavailable" in compacted.turns[0].text.lower()       # noted
    assert "todo app" in compacted.turns[0].text                  # kept more verbatim


def test_compaction_preserves_recent_turn_ids(monkeypatch):
    """A memory entry's provenance ("transcript:tN") must still resolve against the
    compacted record, so recent verbatim turns keep their ORIGINAL ids."""
    brain = _Brain()
    monkeypatch.setattr(tr, "call_model", brain)
    t = _long_transcript()
    recent_ids = [x.id for x in t.turns[-3:]]
    older_ids = [x.id for x in t.turns[:-3]]

    compacted = compact_transcript(t, keep_recent=3, client=object())

    assert [x.id for x in compacted.turns[1:]] == recent_ids   # recent ids preserved
    assert compacted.turns[0].refs == older_ids                # summary refs the folded ids


def test_compaction_does_not_use_memory_compacted_context(monkeypatch):
    """Structural guard: the transcript narrative must not be routed through plan
    memory's dense compactor (that would wreck human scroll-back)."""
    import relay.memory as mem

    def explode(*args, **kwargs):
        raise AssertionError("transcript compaction must NOT call memory.compacted_context")

    monkeypatch.setattr(mem.PlanMemory, "compacted_context", explode)
    brain = _Brain()
    monkeypatch.setattr(tr, "call_model", brain)
    compact_transcript(_long_transcript(), keep_recent=3, client=object())  # no AssertionError


# --- window-bounded brain reads ---------------------------------------------


def test_render_for_brain_fits_whole_thread_when_small(monkeypatch):
    brain = _Brain()
    monkeypatch.setattr(tr, "call_model", brain)
    t = Transcript()
    t.record("user", "planning", "short goal")
    t.record("brain", "proposal", "short plan")
    out = render_for_brain(t, budget_tokens=10_000, client=object())
    assert "short goal" in out and "short plan" in out
    assert brain.calls == 0  # everything fit -> no narration


def test_render_for_brain_stays_under_tiny_window(monkeypatch):
    brain = _Brain(reply="A brief readable recap of the earlier turns.")
    monkeypatch.setattr(tr, "call_model", brain)
    t = _long_transcript(20)
    budget = 60  # tiny simulated window
    out = render_for_brain(t, budget_tokens=budget, client=object())

    assert estimate_tokens(out) <= budget          # provably under cap
    assert out                                      # not empty
    assert brain.calls >= 1                         # older turns were narrated
    # The most-recent turn is represented (recent-verbatim half of the read).
    assert "result" in out or "commit" in out


def test_render_for_brain_empty_is_blank():
    assert render_for_brain(Transcript(), budget_tokens=1000) == ""
    assert render_for_brain(_long_transcript(), budget_tokens=0) == ""
