"""Hermetic tests for model bake-off (C1 / ``relay duel``)."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

from relay.duel import (
    Pairing,
    git_is_dirty,
    git_restore_worktree,
    list_duels,
    load_matrix,
    parse_pair,
    persist_duel,
    run_duel,
)
from relay.loop import STATUS_COMPLETED
from relay.planner import Plan


def _resp(content):
    usage = SimpleNamespace(prompt_tokens=4, completion_tokens=2, total_tokens=6, cost=0.00001)
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))], usage=usage)


class _Completions:
    def __init__(self, brain, hands):
        self.brain = list(brain)
        self.hands = list(hands)
        self.calls: list[dict] = []

    def create(self, *, model, **kwargs):
        self.calls.append({"model": model, **kwargs})
        # Route by whichever queue still has replies for this model family.
        if model.startswith("brain/") or "brain" in model:
            queue = self.brain
            role = "brain"
        else:
            queue = self.hands
            role = "hands"
        assert queue, f"ran out of {role} replies for {model}"
        return _resp(queue.pop(0))


class RoutedClient:
    def __init__(self, brain=(), hands=()):
        self.chat = SimpleNamespace(completions=_Completions(brain, hands))


def test_parse_pair():
    p = parse_pair("brain=vendor/a,hands=vendor/b")
    assert p == Pairing(brain="vendor/a", hands="vendor/b")
    p2 = parse_pair("  brain = x/y , hands = z/w  ")
    assert p2.brain == "x/y" and p2.hands == "z/w"


def test_parse_pair_rejects_bad():
    import pytest

    with pytest.raises(ValueError):
        parse_pair("brain=only")


def test_load_matrix_json_and_text(tmp_path):
    j = tmp_path / "m.json"
    j.write_text(
        json.dumps({"pairings": [{"brain": "a/b", "hands": "c/d"}, {"brain": "e/f", "hands": "g/h"}]}),
        encoding="utf-8",
    )
    pairs = load_matrix(j)
    assert len(pairs) == 2
    assert pairs[0].brain == "a/b"

    t = tmp_path / "m.txt"
    t.write_text("# comment\nbrain=x,hands=y\nbrain=p,hands=q\n", encoding="utf-8")
    assert [p.label() for p in load_matrix(t)] == [
        "brain=x,hands=y",
        "brain=p,hands=q",
    ]


def test_duel_two_pairings_scorecard(tmp_path):
    """Minimal acceptance: compare 2 pairings with RoutedClient."""
    plan = Plan.from_instructions(["create out.txt with OK"])
    # Enough replies for two pairings (plan skipped via committed_plan; hands once each).
    client = RoutedClient(
        brain=[],  # committed_plan skips make_plan
        hands=[
            '<edit path="out.txt">OK</edit>\n<done>wrote</done>',
            '<edit path="out.txt">OK</edit>\n<done>wrote</done>',
        ],
    )
    pairings = [
        Pairing(brain="brain/one", hands="hands/one"),
        Pairing(brain="brain/two", hands="hands/two"),
    ]
    result = run_duel(
        "write out.txt",
        tmp_path,
        pairings,
        client=client,
        supervise=False,
        committed_plan=plan,
        require_clean=False,
        persist=True,
    )
    assert len(result.pairings) == 2
    assert all(s.status == STATUS_COMPLETED for s in result.pairings)
    assert result.pairings[0].brain == "brain/one"
    assert result.pairings[1].hands == "hands/two"
    assert result.pairings[0].steps == 1
    assert result.pairings[0].wall_time_s >= 0
    saved = list_duels(tmp_path)
    assert len(saved) == 1
    assert saved[0]["duel_id"] == result.duel_id
    assert (tmp_path / ".relay" / "duels" / f"{result.duel_id}.json").is_file()


def test_duel_refuses_dirty_git(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "t"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    (tmp_path / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    (tmp_path / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    assert git_is_dirty(tmp_path)

    result = run_duel(
        "noop",
        tmp_path,
        [Pairing(brain="b", hands="h")],
        require_clean=True,
        persist=False,
        run_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not run")),
    )
    assert result.pairings[0].status == "refused_dirty"


def test_git_restore_between_pairings(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "t"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    (tmp_path / "a.txt").write_text("clean\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    (tmp_path / "a.txt").write_text("mutated\n", encoding="utf-8")
    (tmp_path / "extra.txt").write_text("untracked\n", encoding="utf-8")
    assert git_is_dirty(tmp_path)
    assert git_restore_worktree(tmp_path)
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "clean\n"
    assert not (tmp_path / "extra.txt").exists()
    assert not git_is_dirty(tmp_path)


def test_persist_roundtrip(tmp_path):
    from relay.duel import DuelResult, PairingScore

    dr = DuelResult(
        schema_version=1,
        duel_id="test-id",
        timestamp="2026-01-01T00:00:00+00:00",
        goal="g",
        root=str(tmp_path),
        pairings=[
            PairingScore(
                brain="b", hands="h", status="completed",
                steps=2, cost_usd=0.01, escalations=0, wall_time_s=1.5,
            )
        ],
    )
    path = persist_duel(dr, tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["pairings"][0]["cost_usd"] == 0.01
