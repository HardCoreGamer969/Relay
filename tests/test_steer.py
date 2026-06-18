"""v0.0.28 Part 2: steer = replan the remainder with the user's input folded in.

A steer is not a raw injection -- it goes through the BRAIN. EngineRunner.start_steer
invokes the existing `replan` with the steer text as new direction, then executes the
revised remainder (completed steps are NOT redone). It reuses the session transcript
+ memory and counts as a plan revision. These tests drive the bridge headlessly with
a deterministic fake client. Network-free.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from relay.bridge import EngineRunner, Session
from relay.config import ModelConfig
from relay.loop import STATUS_COMPLETED
from relay.planner import Plan, replan

CFG = ModelConfig(brain="vendor/brain", hands="vendor/hands")
WAIT_S = 5.0


def _wait(predicate, timeout=WAIT_S):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def _resp(content):
    usage = SimpleNamespace(prompt_tokens=6, completion_tokens=4, total_tokens=10, cost=0.00002)
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))], usage=usage)


class _SteerClient:
    """Routes by model. The brain's replan call returns a revised remainder plan; the
    hands executes each revised step. Records every call so tests can assert the steer
    text reached replan."""

    def __init__(self, *, revised_steps, hands):
        self._revised = "".join(f"<step>{s}</step>" for s in revised_steps)
        self._hands = list(hands)
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, *, model, messages, **kwargs):
        self.calls.append({"model": model, "messages": messages})
        if model == "vendor/hands":
            return _resp(self._hands.pop(0))
        system = " ".join(messages[0]["content"].split())
        if "Revise the plan for the REMAINING work" in system:  # the replan call
            return _resp(f"<plan>{self._revised}</plan>")
        return _resp("<verdict>accept</verdict>")  # supervise is off, but be safe

    def replan_user_messages(self):
        out = []
        for c in self.calls:
            if c["model"] != "vendor/brain":
                continue
            sys = " ".join(c["messages"][0]["content"].split())
            if "Revise the plan for the REMAINING work" in sys:
                out.append(c["messages"][1]["content"])
        return out


def _prior_plan(tmp_path):
    """A plan with step A done and step B still pending (the interrupted remainder)."""
    plan = Plan.from_instructions(["create A.txt", "create B.txt"])
    plan.mark_done(plan.steps[0], "A.txt created")
    (tmp_path / "A.txt").write_text("done", encoding="utf-8")
    return plan


def _runner(tmp_path, client, session, **kwargs):
    return EngineRunner(
        tmp_path, models=CFG, client=client,
        on_request=lambda r: None, on_event=lambda e: None, on_finished=lambda o: None,
        transcript=session.transcript, memory=session.memory,
        run_kwargs={"supervise": False, "max_total_steps": 20, **kwargs},
    )


# --- replan steer framing (unit) --------------------------------------------


def test_replan_steer_frames_the_user_direction():
    client = _SteerClient(revised_steps=["use Postgres for storage"], hands=[])
    plan = Plan.from_instructions(["use SQLite"])
    revised = replan(
        "build the app", plan, plan.steps[0], "user interrupted and redirected",
        plan.completed_outcomes(), models=CFG, client=client, steer="use Postgres instead",
    )
    assert revised is not None and revised.steps[0].instruction == "use Postgres for storage"
    user_msg = client.replan_user_messages()[0]
    assert "use Postgres instead" in user_msg          # the steer text was folded in
    assert "INTERRUPTED and gave new direction" in user_msg  # redirection framing, not failure


# --- the steer continuation through EngineRunner -----------------------------


def test_steer_replans_remainder_and_continues(tmp_path):
    session = Session(tmp_path)
    session.goal = "build files"
    prior = _prior_plan(tmp_path)
    client = _SteerClient(
        revised_steps=["create C.txt instead of B"],
        hands=['<write path="C.txt">c</write>\n<done>made C</done>'],
    )
    outcome_box = {}
    runner = EngineRunner(
        tmp_path, models=CFG, client=client,
        on_request=lambda r: None, on_event=lambda e: None,
        on_finished=lambda o: outcome_box.setdefault("o", o),
        transcript=session.transcript, memory=session.memory,
        run_kwargs={"supervise": False, "max_total_steps": 20},
    )
    runner.start_steer(session.goal, prior, "actually create C.txt, not B.txt")

    assert _wait(lambda: "o" in outcome_box)
    outcome = outcome_box["o"]
    assert outcome.status == STATUS_COMPLETED
    # replan got the steer text as new direction.
    assert any("create C.txt, not B.txt" in m for m in client.replan_user_messages())
    # Execution continued on the REVISED plan (C.txt created).
    assert (tmp_path / "C.txt").read_text(encoding="utf-8") == "c"
    # The completed step was NOT redone (B.txt was never created; A.txt untouched).
    assert not (tmp_path / "B.txt").exists()
    assert runner.join(WAIT_S)


def test_steer_reuses_session_transcript(tmp_path):
    """The steer continuation runs on the SAME session transcript (one thread)."""
    session = Session(tmp_path)
    session.transcript.record("user", "goal", "build files")
    prior = _prior_plan(tmp_path)
    client = _SteerClient(
        revised_steps=["create C.txt"],
        hands=['<write path="C.txt">c</write>\n<done>made C</done>'],
    )
    runner = _runner(tmp_path, client, session)
    runner.start_steer("build files", prior, "make C instead")
    assert _wait(lambda: not runner.is_running and runner.outcome is not None)
    assert runner.transcript is session.transcript
    # The original turn is still present (continuation, not a fresh thread).
    assert any(t.text == "build files" for t in session.transcript.turns)


def test_steer_counts_as_a_plan_revision(tmp_path):
    """The session tracks steers as revisions, bounded by max_plan_revisions."""
    session = Session(tmp_path)
    assert session.revisions == 0
    session.note_steer()
    assert session.revisions == 1
    assert session.can_steer(max_revisions=1) is False
