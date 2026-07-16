"""Hermetic tests for plan forks + checkpoints (D2)."""

from __future__ import annotations

from types import SimpleNamespace

from relay.config import ModelConfig
from relay.orchestrator import STATUS_COMPLETED, run_planned
from relay.plan_fork import (
    list_checkpoints,
    list_forks,
    load_checkpoint,
    load_fork,
    plan_for_resume,
    save_checkpoint,
    save_fork,
)
from relay.planner import Plan, PlanStep

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

    def create(self, *, model, **kwargs):
        queue = self.brain if model == "vendor/brain" else self.hands
        assert queue, f"ran out of replies for {model}"
        return _resp(queue.pop(0))


class RoutedClient:
    def __init__(self, brain=(), hands=()):
        self.chat = SimpleNamespace(completions=_Completions(brain, hands))


def test_fork_save_load_roundtrip(tmp_path):
    plan = Plan.from_instructions(["write a.txt", "write b.txt"])
    save_fork(tmp_path, "alt-a", plan, goal="two files", notes="minimal fix")
    save_fork(tmp_path, "alt-b", plan.copy(), goal="two files", notes="refactor")
    rows = list_forks(tmp_path)
    assert {r["name"] for r in rows} == {"alt-a", "alt-b"}
    loaded = load_fork(tmp_path, "alt-a")
    assert loaded.goal == "two files"
    assert loaded.to_plan().steps[0].instruction == "write a.txt"
    # Executing B must not destroy A.
    again = load_fork(tmp_path, "alt-a")
    assert again.name == "alt-a"
    assert len(list_forks(tmp_path)) == 2


def test_checkpoint_restores_cursor(tmp_path):
    plan = Plan(steps=[
        PlanStep(0, "write a.txt", status="done", outcome="ok"),
        PlanStep(1, "write b.txt", status="pending"),
        PlanStep(2, "write c.txt", status="pending"),
    ])
    cp = save_checkpoint(tmp_path, plan, goal="abc", step_touches={"0": ["a.txt"]})
    assert cp.cursor == 1
    assert cp.completed_indices == [0]
    loaded = load_checkpoint(tmp_path, cp.id)
    resumed = plan_for_resume(loaded)
    assert resumed.steps[0].status == "done"
    assert resumed.next_pending().index == 1
    assert resumed.next_pending().instruction == "write b.txt"
    latest = load_checkpoint(tmp_path, "latest")
    assert latest.id == cp.id


def test_run_planned_checkpoint_and_resume(tmp_path):
    """One step completes → checkpoint; resume skips done step."""
    plan = Plan.from_instructions(["create hello.txt with hi", "create bye.txt with bye"])
    # Step 0: write + done. Review accept.
    client = RoutedClient(
        brain=[
            "<verdict>accept</verdict><reason>ok</reason>",
        ],
        hands=[
            '<write path="hello.txt">hi</write>\n<done>wrote hello</done>',
        ],
    )
    result = run_planned(
        "two files",
        tmp_path,
        models=CFG,
        client=client,
        committed_plan=plan,
        supervise=True,
        max_total_steps=10,
        auto_checkpoint=True,
        # Stop after first step by cancelling before step 1.
        cancel_check=lambda: (tmp_path / "hello.txt").exists()
        and not (tmp_path / ".relay" / "stop").exists()
        and _mark_stop(tmp_path),
    )
    # cancel_check fires at next boundary after step 0 → cancelled with checkpoint
    assert (tmp_path / "hello.txt").read_text() == "hi"
    cps = list_checkpoints(tmp_path)
    assert cps, "expected a checkpoint after step 0"
    cp = load_checkpoint(tmp_path, "latest")
    assert 0 in cp.completed_indices
    resumed_plan = plan_for_resume(cp)
    assert resumed_plan.steps[0].status == "done"
    assert resumed_plan.next_pending().instruction.startswith("create bye")

    client2 = RoutedClient(
        brain=["<verdict>accept</verdict><reason>ok</reason>"],
        hands=[
            '<write path="bye.txt">bye</write>\n<done>wrote bye</done>',
        ],
    )
    result2 = run_planned(
        "two files",
        tmp_path,
        models=CFG,
        client=client2,
        committed_plan=resumed_plan,
        supervise=True,
        auto_checkpoint=True,
    )
    assert result2.status == STATUS_COMPLETED
    assert (tmp_path / "bye.txt").read_text() == "bye"
    # First file untouched by resume.
    assert (tmp_path / "hello.txt").read_text() == "hi"


def _mark_stop(root):
    (root / ".relay" / "stop").write_text("1", encoding="utf-8")
    return True


def test_save_fork_as_on_run(tmp_path):
    plan = Plan.from_instructions(["create x.txt with x"])
    client = RoutedClient(
        brain=["<verdict>accept</verdict><reason>ok</reason>"],
        hands=['<write path="x.txt">x</write>\n<done>ok</done>'],
    )
    run_planned(
        "x",
        tmp_path,
        models=CFG,
        client=client,
        committed_plan=plan,
        supervise=True,
        save_fork_as="pre-x",
        auto_checkpoint=False,
    )
    fork = load_fork(tmp_path, "pre-x")
    assert fork.to_plan().steps[0].instruction == "create x.txt with x"
