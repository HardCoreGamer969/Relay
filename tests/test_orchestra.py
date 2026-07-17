"""Hermetic tests for orchestra mode (D4)."""

from __future__ import annotations

from types import SimpleNamespace

from relay.config import ModelConfig
from relay.orchestra import (
    PathLease,
    claims_overlap,
    extract_path_claims,
    select_disjoint_batch,
)
from relay.orchestrator import STATUS_COMPLETED, run_planned
from relay.planner import Plan, PlanStep
from relay.telemetry import Ledger

CFG = ModelConfig(brain="vendor/brain", hands="vendor/hands")


def _resp(content, cost=0.00001):
    usage = SimpleNamespace(
        prompt_tokens=4, completion_tokens=2, total_tokens=6, cost=cost
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=usage,
    )


class _Completions:
    def __init__(self, brain, hands):
        self.brain = list(brain)
        self.hands = list(hands)
        self.calls: list[dict] = []

    def create(self, *, model, **kwargs):
        self.calls.append({"model": model, **kwargs})
        # Both hands and hands-N resolve to vendor/hands via ModelConfig.
        queue = self.brain if model == "vendor/brain" else self.hands
        assert queue, f"ran out of replies for {model}"
        return _resp(queue.pop(0))


class RoutedClient:
    def __init__(self, brain=(), hands=()):
        self.chat = SimpleNamespace(completions=_Completions(brain, hands))


def test_extract_path_claims():
    assert "a.txt" in extract_path_claims("create a.txt with hello")
    assert "src/b.py" in extract_path_claims("edit src/b.py to add foo")
    assert extract_path_claims("think about architecture") == set()


def test_claims_overlap_and_batch():
    steps = [
        PlanStep(0, "create a.txt with a"),
        PlanStep(1, "create b.txt with b"),
        PlanStep(2, "create a.txt again"),  # overlaps 0
    ]
    batch = select_disjoint_batch(steps, max_workers=3)
    assert len(batch) == 2
    assert {s.index for s in batch} == {0, 1}
    assert claims_overlap(
        extract_path_claims(steps[0].instruction),
        extract_path_claims(steps[2].instruction),
    )


def test_path_lease_conflict():
    lease = PathLease()
    ok, _ = lease.try_claim("hands-1", ["a.txt"])
    assert ok
    ok2, contested = lease.try_claim("hands-2", ["a.txt"])
    assert not ok2
    assert contested == "a.txt"
    lease.release("hands-1")
    ok3, _ = lease.try_claim("hands-2", ["a.txt"])
    assert ok3


def test_orchestra_two_disjoint_steps(tmp_path):
    plan = Plan.from_instructions([
        "create left.txt with L",
        "create right.txt with R",
    ])
    # Orchestra skips supervise for the batch — no brain reviews needed for success.
    client = RoutedClient(
        brain=[],
        hands=[
            '<write path="left.txt">L</write>\n<done>left</done>',
            '<write path="right.txt">R</write>\n<done>right</done>',
        ],
    )
    ledger = Ledger()
    result = run_planned(
        "two files",
        tmp_path,
        models=CFG,
        client=client,
        ledger=ledger,
        committed_plan=plan,
        supervise=False,
        orchestra_workers=2,
        auto_checkpoint=False,
    )
    assert result.status == STATUS_COMPLETED
    assert (tmp_path / "left.txt").read_text() == "L"
    assert (tmp_path / "right.txt").read_text() == "R"
    assert any(e.kind == "orchestra_batch" for e in result.events)
    roles = {r.role for r in ledger.records}
    assert "hands-1" in roles or "hands-2" in roles
    # ModelConfig maps hands-N → hands model.
    assert CFG.for_role("hands-2") == "vendor/hands"


def test_orchestra_overlapping_claims_detected():
    """select_disjoint_batch excludes overlapping steps from the parallel batch."""
    steps = [
        PlanStep(0, "write shared.txt part one"),
        PlanStep(1, "write shared.txt part two"),
    ]
    batch = select_disjoint_batch(steps, max_workers=2)
    assert batch == []  # both claim shared.txt → no parallel batch
