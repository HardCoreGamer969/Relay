"""Network-free tests for the ONE continuous thread (conversation -> loop).

A single :class:`~relay.transcript.Transcript` is shared by the planning
conversation and the autonomous loop, so a mid-run escalation is the next turn of
the SAME dialogue. The fake client routes by model (brain/hands) and, for the
brain, by system prompt -- handling scope, propose, answer/escalate, review, and
(if it fires) narrative compaction. No network.
"""

from __future__ import annotations

from types import SimpleNamespace

from brain_fakes import is_reaction_call, reaction_for_messages
from relay.config import ModelConfig
from relay.conversation import plan_conversationally
from relay.loop import STATUS_COMPLETED
from relay.memory import MemoryBus
from relay.orchestrator import run_planned
from relay.planner import Plan
from relay.transcript import Transcript

CFG = ModelConfig(brain="vendor/brain", hands="vendor/hands")


def _resp(content):
    usage = SimpleNamespace(prompt_tokens=6, completion_tokens=4, total_tokens=10, cost=0.00002)
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))], usage=usage)


class _Client:
    """Routes by model, then (for the brain) by system prompt. Records every call."""

    def __init__(self, *, hands=None, escalate=True):
        self._hands = list(hands or [
            "<question>do we need OAuth login?</question>",
            '<edit path="login.py">x</edit>\n<done>added login</done>',
        ])
        self._hands_i = 0
        self._escalate = escalate
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, *, model, messages, **kwargs):
        self.calls.append({"model": model, "messages": messages, **kwargs})
        system = " ".join(messages[0]["content"].split())
        if model == "vendor/hands":
            reply = self._hands[self._hands_i]
            self._hands_i += 1
            return _resp(reply)
        # brain calls, dispatched by their system prompt:
        if "assess the goal's SCOPE" in system:
            return _resp("<scope>small</scope><reason>self-contained</reason>")
        if "precise, executor-ready plan" in system:
            return _resp("<plan><step>add login</step></plan>")
        if is_reaction_call(system):
            return _resp(reaction_for_messages(messages))
        if "asked a question mid-step" in system:
            if self._escalate:
                return _resp("<decision>escalate</decision><ask_user>Should login support "
                             "OAuth, given you wanted it simple?</ask_user>")
            return _resp("<decision>self_answer</decision><answer>use session cookies</answer>")
        if "readable narrative" in system.lower():
            return _resp("Earlier, you asked for login and committed; then mid-run I asked "
                         "about OAuth and you decided.")
        if "supervising an EXECUTOR" in system:
            return _resp("<verdict>accept</verdict>")
        return _resp("<verdict>accept</verdict>")

    def brain_calls(self, marker):
        marker = marker.lower()
        return [c for c in self.calls if c["model"] == "vendor/brain"
                and marker in " ".join(c["messages"][0]["content"].split()).lower()]


def _scripted(*replies):
    queue = list(replies)

    def turn(prompt):
        return queue.pop(0) if queue else "ok"

    return turn


def _converse_then_run(tmp_path, *, user_decision, memory=None, escalate=True):
    """The full arc on ONE transcript: plan as a conversation, then execute."""
    client = _Client(escalate=escalate)
    transcript = Transcript()
    conversation = plan_conversationally(
        "add login", tmp_path, models=CFG, client=client,
        user_turn=_scripted("ok"), transcript=transcript,
    )
    assert conversation.committed and conversation.transcript is transcript
    result = run_planned(
        "add login", tmp_path, models=CFG, client=client,
        committed_plan=conversation.plan, user_decision=user_decision,
        memory=memory, transcript=transcript,
    )
    return client, transcript, result


# --- one continuous thread --------------------------------------------------


def test_planning_and_escalation_live_in_one_ordered_thread(tmp_path):
    asked = []
    client, transcript, result = _converse_then_run(
        tmp_path, user_decision=lambda q: asked.append(q) or "yes, Google OAuth"
    )

    assert result.status == STATUS_COMPLETED
    assert result.transcript is transcript           # the SAME object end to end
    assert asked                                      # the escalation reached the user

    phases = [t.phase for t in transcript.turns]
    assert {"proposal", "commit", "escalation", "decision"} <= set(phases)
    # Chronological: the escalation is a continuation AFTER the commit, and the
    # user's decision follows the escalation -- not a context-less popup up front.
    assert phases.index("commit") < phases.index("escalation") < phases.index("decision")
    # created_at is a strictly increasing single ordinal sequence across both phases.
    assert [t.created_at for t in transcript.turns] == sorted(t.created_at for t in transcript.turns)


def test_escalation_is_phrased_with_conversation_context(tmp_path):
    client, transcript, result = _converse_then_run(tmp_path, user_decision=lambda q: "ok")

    # The brain's escalate decision was made WITH the conversation so far in hand.
    answer_calls = client.brain_calls("asked a question mid-step")
    assert answer_calls
    prompt = "\n".join(m["content"] for m in answer_calls[0]["messages"])
    assert "conversation so far" in prompt.lower()        # the context block is present
    assert "brain (proposal)" in prompt                   # ...and holds real earlier turns
    # No model-side narrative was needed for this short thread (it fit verbatim).
    assert not client.brain_calls("readable narrative")


# --- transcript-first, memory-derived (across the live loop) ----------------


def test_escalation_decision_is_authoritative_turn_with_linked_memory(tmp_path):
    memory = MemoryBus()
    client, transcript, result = _converse_then_run(
        tmp_path, user_decision=lambda q: "yes, Google OAuth", memory=memory
    )

    decision_turns = [t for t in transcript.turns if t.phase == "decision"]
    assert len(decision_turns) == 1                       # the authoritative record
    # v0.0.29: the user confirmation is an authoritative directive, so it graduates to
    # the SHARED pool (where the hands can see it), NOT the brain pool.
    confirmations = [e for e in memory.shared.entries if e.kind == "confirmation"]
    assert not [e for e in memory.brain.entries if e.kind == "confirmation"]
    # The memory entry is DERIVED from the turn: provenance points back at it.
    assert any(c.provenance == f"transcript:{decision_turns[0].id}" for c in confirmations)
    assert any("OAuth" in c.detail for c in confirmations)


# --- conversation, not an event dump ----------------------------------------


def test_transcript_is_conversation_not_per_tool_call_dump(tmp_path):
    client, transcript, result = _converse_then_run(tmp_path, user_decision=lambda q: "ok")

    assert (tmp_path / "login.py").exists()              # a tool call (edit) really happened
    event_kinds = [e.kind for e in result.events]
    assert "exec_action" in event_kinds                  # granular execution IS in the Event stream
    # ...but the transcript holds only conversational phases -- no per-tool-call turns.
    conversational = {"planning", "proposal", "reaction", "commit",
                      "escalation", "decision", "result", "summary"}
    assert {t.phase for t in transcript.turns} <= conversational
    assert all(t.phase != "exec_action" for t in transcript.turns)
    assert len(transcript.turns) < 8                      # conversation-sized, not action-sized


# --- post-execution compaction is produced ----------------------------------


def test_post_execution_compaction_is_available_and_readable(tmp_path):
    client, transcript, result = _converse_then_run(tmp_path, user_decision=lambda q: "ok")
    assert result.transcript_compacted is not None
    # This short thread fits, so the compacted record equals the live thread (no fold).
    assert [t.text for t in result.transcript_compacted.turns] == [t.text for t in transcript.turns]
    # A closing, human-readable result turn exists in the thread.
    assert any(t.phase == "result" for t in transcript.turns)


def test_post_execution_pass_folds_a_long_thread(tmp_path):
    # A long pre-existing thread (a long conversation) is folded by the post-exec
    # pass: older turns become ONE readable narrative, recent turns stay verbatim.
    client = _Client(hands=['<edit path="x.txt">X</edit>\n<done>did it</done>'])
    transcript = Transcript()
    for i in range(10):
        transcript.record("user" if i % 2 else "brain", "reaction", f"older planning turn {i}")

    result = run_planned(
        "g", tmp_path, models=CFG, client=client,
        committed_plan=Plan.from_instructions(["create x.txt"]),
        transcript=transcript,
    )

    assert result.status == STATUS_COMPLETED
    compacted = result.transcript_compacted
    assert compacted.turns[0].phase == "summary"             # older turns folded
    assert "earlier" in compacted.turns[0].text.lower()      # readable narrative prose
    assert len(compacted.turns) < len(result.transcript.turns)
    assert client.brain_calls("readable narrative")          # narration actually fired
