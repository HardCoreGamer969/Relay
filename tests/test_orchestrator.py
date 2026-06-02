"""Network-free tests for the two-role orchestrator (brain + hands).

A scripted client returns replies in order; both roles route through it, so the
sequence of replies follows the deterministic phase order: make_plan (brain) ->
each executor step (hands) -> replan (brain) on escalation, etc. Calls are
distinguished by the recorded ``model`` (brain vs hands).
"""

from __future__ import annotations

from types import SimpleNamespace

from relay.config import ModelConfig
from relay.loop import STATUS_COMPLETED, STATUS_MAX_STEPS
from relay.orchestrator import (
    STATUS_ABORTED_BY_BRAIN,
    STATUS_ESCALATION_LIMIT,
    STATUS_PLANNING_FAILED,
    run_planned,
)
from relay.telemetry import Ledger

CFG = ModelConfig(brain="vendor/brain", hands="vendor/hands")


def _resp(content):
    usage = SimpleNamespace(prompt_tokens=6, completion_tokens=4, total_tokens=10, cost=0.00002)
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))], usage=usage)


class _Completions:
    def __init__(self, replies):
        self._replies = list(replies)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        assert self._replies, "scripted client ran out of replies"
        return _resp(self._replies.pop(0))


class ScriptedClient:
    def __init__(self, replies):
        self.chat = SimpleNamespace(completions=_Completions(replies))

    @property
    def calls(self):
        return self.chat.completions.calls


def _hands_calls(client):
    return [c for c in client.calls if c["model"] == "vendor/hands"]


def _brain_calls(client):
    return [c for c in client.calls if c["model"] == "vendor/brain"]


def test_happy_path_completes_and_writes_files(tmp_path):
    client = ScriptedClient(
        [
            "<plan><step>create alpha.txt with ALPHA</step><step>create beta.txt with BETA</step></plan>",
            '<edit path="alpha.txt">ALPHA</edit>\n<done>created alpha.txt</done>',
            '<edit path="beta.txt">BETA</edit>\n<done>created beta.txt</done>',
        ]
    )
    ledger = Ledger()
    result = run_planned("make two files", tmp_path, models=CFG, ledger=ledger, client=client)

    assert result.status == STATUS_COMPLETED
    assert result.done is True
    assert [s.status for s in result.plan.steps] == ["done", "done"]
    assert (tmp_path / "alpha.txt").read_text(encoding="utf-8") == "ALPHA"
    assert (tmp_path / "beta.txt").read_text(encoding="utf-8") == "BETA"


def test_telemetry_split_across_brain_and_hands(tmp_path):
    client = ScriptedClient(
        [
            "<plan><step>create a.txt</step></plan>",
            '<edit path="a.txt">A</edit>\n<done>created a.txt</done>',
        ]
    )
    ledger = Ledger()
    run_planned("g", tmp_path, models=CFG, ledger=ledger, client=client)

    by_role = ledger.by_role()
    assert set(by_role) == {"brain", "hands"}
    assert by_role["brain"].model == "vendor/brain"
    assert by_role["hands"].model == "vendor/hands"
    assert by_role["brain"].calls == 1  # one plan call
    assert by_role["hands"].calls == 1  # one executor step


def test_executor_context_is_narrow(tmp_path):
    """The crucial guard: each step's executor context excludes the full plan and
    prior steps' raw transcripts -- only the current step + one-line carry-over."""
    client = ScriptedClient(
        [
            "<plan><step>STEP_ALPHA create alpha.txt</step><step>STEP_BETA create beta.txt</step></plan>",
            '<edit path="alpha.txt">ALPHA_RAW_CONTENT</edit>\n<done>created alpha.txt</done>',
            '<edit path="beta.txt">BETA</edit>\n<done>created beta.txt</done>',
        ]
    )
    result = run_planned("g", tmp_path, models=CFG, ledger=Ledger(), client=client)
    assert result.status == STATUS_COMPLETED

    hands = _hands_calls(client)
    assert len(hands) == 2

    def joined(call):
        return "\n".join(m["content"] for m in call["messages"])

    step0_ctx = joined(hands[0])
    step1_ctx = joined(hands[1])

    # Step 0's context has its own instruction but NOT the future step (no full plan leak).
    assert "STEP_ALPHA" in step0_ctx
    assert "STEP_BETA" not in step0_ctx

    # Step 1's context: its own instruction + step 0's one-line OUTCOME, but NOT
    # step 0's raw transcript and NOT step 0's instruction.
    assert "STEP_BETA" in step1_ctx
    assert "created alpha.txt" in step1_ctx       # one-line carry-over is allowed
    assert "ALPHA_RAW_CONTENT" not in step1_ctx   # prior raw transcript NOT leaked
    assert "STEP_ALPHA" not in step1_ctx          # prior step instruction NOT leaked


def test_escalation_then_completion(tmp_path):
    client = ScriptedClient(
        [
            "<plan><step>STEP0 create a.txt</step><step>STEP1 do the hard thing</step></plan>",
            '<edit path="a.txt">A</edit>\n<done>created a.txt</done>',  # step 0 done
            "<blocked>cannot do the hard thing this way</blocked>",      # step 1 -> escalate
            "<plan><step>STEP1B create b.txt the easy way</step></plan>",  # replan
            '<edit path="b.txt">B</edit>\n<done>did it the easy way</done>',  # revised step done
        ]
    )
    ledger = Ledger()
    result = run_planned("g", tmp_path, models=CFG, ledger=ledger, client=client, max_escalations=3)

    assert result.status == STATUS_COMPLETED
    assert result.escalations == 1
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "A"
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "B"

    # Completed step 0 kept its outcome and was NOT redone: exactly 3 hands calls
    # (step0, blocked step1, revised step) -- a redo would be 4+.
    assert len(_hands_calls(client)) == 3
    assert any(s.status == "done" and s.outcome == "created a.txt" for s in result.plan.steps)
    assert any(s.status == "failed" for s in result.plan.steps)  # the blocked step is recorded


def test_escalation_limit(tmp_path):
    client = ScriptedClient(
        [
            "<plan><step>do X</step></plan>",
            "<blocked>nope</blocked>",                       # step fails -> escalate(1)
            "<plan><step>do X differently</step></plan>",    # replan
            "<blocked>still nope</blocked>",                 # fails again -> over the limit
        ]
    )
    result = run_planned("g", tmp_path, models=CFG, ledger=Ledger(), client=client, max_escalations=1)
    assert result.status == STATUS_ESCALATION_LIMIT
    assert result.escalations == 1


def test_brain_aborts_on_escalation(tmp_path):
    client = ScriptedClient(
        [
            "<plan><step>do X</step></plan>",
            "<blocked>cannot proceed</blocked>",     # step fails -> escalate
            "<abort>this goal is unreachable</abort>",  # brain aborts the replan
        ]
    )
    result = run_planned("g", tmp_path, models=CFG, ledger=Ledger(), client=client)
    assert result.status == STATUS_ABORTED_BY_BRAIN


def test_planning_failed(tmp_path):
    client = ScriptedClient(["no plan here", "still none", "nope", "nope"])
    result = run_planned("g", tmp_path, models=CFG, ledger=Ledger(), client=client)
    assert result.status == STATUS_PLANNING_FAILED
    assert result.plan is None


def test_overall_budget_exhausted_is_max_steps(tmp_path):
    client = ScriptedClient(
        [
            "<plan><step>create a.txt</step><step>create b.txt</step></plan>",
            '<edit path="a.txt">A</edit>\n<done>done a</done>',  # step 0 uses the 1 allowed call
        ]
    )
    result = run_planned(
        "g", tmp_path, models=CFG, ledger=Ledger(), client=client, max_total_steps=1
    )
    assert result.status == STATUS_MAX_STEPS
    assert (tmp_path / "a.txt").exists()
    assert not (tmp_path / "b.txt").exists()  # step 1 never ran -- budget exhausted


def test_plan_gate_can_decline_before_execution(tmp_path):
    client = ScriptedClient(["<plan><step>create a.txt</step></plan>"])
    result = run_planned(
        "g", tmp_path, models=CFG, ledger=Ledger(), client=client,
        plan_gate=lambda plan: False,
    )
    assert result.status == "declined_by_user"
    assert not (tmp_path / "a.txt").exists()  # nothing executed
    assert len(_hands_calls(client)) == 0


def test_events_are_logged(tmp_path):
    seen = []
    client = ScriptedClient(
        [
            "<plan><step>create a.txt</step></plan>",
            '<edit path="a.txt">A</edit>\n<done>created a.txt</done>',
        ]
    )
    result = run_planned("g", tmp_path, models=CFG, ledger=Ledger(), client=client, on_event=seen.append)

    kinds = [e.kind for e in result.events]
    assert "plan_created" in kinds
    assert "step_start" in kinds
    assert "step_done" in kinds
    assert kinds == [e.kind for e in seen]  # on_event saw the same stream
