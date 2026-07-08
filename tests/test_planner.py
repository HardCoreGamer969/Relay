"""Network-free tests for the planner (the brain).

A scripted client returns the brain's replies in order; no network is used.
"""

from __future__ import annotations

from types import SimpleNamespace

from relay.config import ModelConfig
from relay.planner import MAX_PLAN_STEPS, Plan, PlanStep, make_plan, replan
from relay.telemetry import Ledger
from relay.tools import Tools

CFG = ModelConfig(brain="vendor/brain", hands="vendor/hands")


def _resp(content):
    usage = SimpleNamespace(prompt_tokens=5, completion_tokens=5, total_tokens=10, cost=0.00001)
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


def test_plan_parses_into_ordered_steps(tmp_path):
    client = ScriptedClient(["<plan><step>do A</step><step>do B</step><step>do C</step></plan>"])
    plan = make_plan("a goal", tmp_path, models=CFG, ledger=Ledger(), client=client)

    assert plan is not None
    assert [s.instruction for s in plan.steps] == ["do A", "do B", "do C"]
    assert [s.index for s in plan.steps] == [0, 1, 2]
    assert all(s.status == "pending" for s in plan.steps)


# --- Plan snapshot support (v0.0.32: to_state / from_state / copy) -----------


def test_plan_to_state_from_state_roundtrip():
    """A plan with mixed statuses round-trips through to_state/from_state."""
    plan = Plan.from_instructions(["step A", "step B", "step C"])
    plan.mark_done(plan.steps[0], "did A")
    plan.mark_failed(plan.steps[1], "B failed")
    # step 2 is still pending

    state = plan.to_state()
    restored = Plan.from_state(state)

    assert len(restored.steps) == 3
    assert [s.instruction for s in restored.steps] == ["step A", "step B", "step C"]
    assert [s.index for s in restored.steps] == [0, 1, 2]
    assert restored.steps[0].status == "done"
    assert restored.steps[0].outcome == "did A"
    assert restored.steps[1].status == "failed"
    assert restored.steps[1].outcome == "B failed"
    assert restored.steps[2].status == "pending"


def test_plan_copy_is_independent():
    """copy() produces an independent plan -- mutating the copy does not affect the
    original (the basis for plan snapshot/fork in steer/queue continuations)."""
    plan = Plan.from_instructions(["do A", "do B"])
    snapshot = plan.copy()

    # Mutate the original
    plan.mark_done(plan.steps[0], "did A")
    plan.steps[1].instruction = "do B (modified)"

    # The snapshot is unaffected
    assert snapshot.steps[0].status == "pending"  # not done
    assert snapshot.steps[0].outcome is None
    assert snapshot.steps[1].instruction == "do B"  # not modified


def test_plan_step_to_state_from_state_roundtrip():
    """A PlanStep round-trips through to_state/from_state."""
    step = PlanStep(index=3, instruction="do X", status="done", outcome="did X")
    state = step.to_state()
    restored = PlanStep.from_state(state)
    assert restored.index == 3
    assert restored.instruction == "do X"
    assert restored.status == "done"
    assert restored.outcome == "did X"


def test_plan_step_from_state_defaults():
    """from_state fills defaults for missing keys (defensive parsing)."""
    step = PlanStep.from_state({"index": 0, "instruction": "do Y"})
    assert step.status == "pending"
    assert step.outcome is None


def test_brain_investigates_readonly_then_plans(tmp_path):
    (tmp_path / "main.py").write_text("print('hi')\n", encoding="utf-8")
    client = ScriptedClient(
        [
            '<list path="."/>',
            '<read path="main.py"/>',
            "<plan><step>add a docstring to main.py</step></plan>",
        ]
    )
    plan = make_plan("document main.py", tmp_path, models=CFG, ledger=Ledger(), client=client)

    assert plan is not None
    assert [s.instruction for s in plan.steps] == ["add a docstring to main.py"]
    assert len(client.chat.completions.calls) == 3  # two investigations + the plan


def test_brain_cannot_edit_or_bash_during_planning(tmp_path):
    client = ScriptedClient(
        [
            '<edit path="x.txt">should not be written</edit>\n<bash>touch y.txt</bash>',
            "<plan><step>do the real thing</step></plan>",
        ]
    )
    plan = make_plan("g", tmp_path, models=CFG, ledger=Ledger(), client=client)

    assert plan is not None
    # The read-only planner's write/exec attempts must not have run.
    assert not (tmp_path / "x.txt").exists()
    assert not (tmp_path / "y.txt").exists()
    # And it was told it is read-only, fed back before its next turn.
    second_turn = client.chat.completions.calls[1]["messages"]
    joined = " ".join(m["content"] for m in second_turn)
    assert "READ-ONLY" in joined


def test_planning_fails_when_no_plan_parses(tmp_path):
    client = ScriptedClient(["just chatting", "still no plan", "nope", "nope", "nope"])
    plan = make_plan("g", tmp_path, models=CFG, ledger=Ledger(), client=client, max_plan_retries=2)
    assert plan is None  # bounded retries, no infinite loop


def test_brain_abort_during_planning_returns_none(tmp_path):
    client = ScriptedClient(["<abort>this goal is incoherent</abort>"])
    plan = make_plan("g", tmp_path, models=CFG, ledger=Ledger(), client=client)
    assert plan is None


def test_plan_step_count_is_capped(tmp_path):
    many = "".join(f"<step>step {i}</step>" for i in range(30))
    client = ScriptedClient([f"<plan>{many}</plan>"])
    plan = make_plan("g", tmp_path, models=CFG, ledger=Ledger(), client=client)
    assert plan is not None
    assert len(plan.steps) == MAX_PLAN_STEPS


def test_replan_returns_revised_remaining_plan(tmp_path):
    plan = Plan.from_instructions(["a", "b"])
    plan.mark_done(plan.steps[0], "did a")
    failed = plan.steps[1]
    plan.mark_failed(failed, "blocked: ran into a wall")

    client = ScriptedClient(["<plan><step>b-prime</step><step>c</step></plan>"])
    revised = replan(
        "g", plan, failed, "blocked: ran into a wall", plan.completed_outcomes(),
        models=CFG, ledger=Ledger(), client=client,
    )

    assert revised is not None
    assert [s.instruction for s in revised.steps] == ["b-prime", "c"]


def test_replan_abort_returns_none(tmp_path):
    failed = PlanStep(index=0, instruction="a", status="failed")
    client = ScriptedClient(["<abort>cannot recover from here</abort>"])
    revised = replan("g", Plan(steps=[failed]), failed, "boom", [], models=CFG, ledger=Ledger(), client=client)
    assert revised is None


# --- v0.06: review_step / answer_or_escalate / evolve_plan ------------------

import relay.planner as planner_mod  # noqa: E402
from relay.memory import PlanMemory  # noqa: E402
from relay.planner import answer_or_escalate, evolve_plan, review_step  # noqa: E402


class _FakeBrain:
    """Records calls and returns a fixed reply as the brain's text."""

    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def __call__(self, role, messages, **kwargs):
        self.calls.append((role, messages, kwargs))
        return SimpleNamespace(text=self.reply, record=None)


def _one_step_plan():
    return Plan.from_instructions(["do the thing"])


def test_review_accept_with_records(monkeypatch, tmp_path):
    monkeypatch.setattr(
        planner_mod, "call_model",
        _FakeBrain('<verdict>accept</verdict><record kind="fact">routes wired :: routes done</record>'),
    )
    plan = _one_step_plan()
    review = review_step("g", plan, plan.steps[0], "did it", ["[edit] wrote x"], PlanMemory(),
                         tools=Tools(tmp_path), models=CFG, client=object())
    assert review.verdict == "accept"
    assert review.records == [("fact", "routes wired", "routes done")]


def test_review_follow_up(monkeypatch, tmp_path):
    monkeypatch.setattr(
        planner_mod, "call_model",
        _FakeBrain("<verdict>follow_up</verdict><followup>add the missing docstring</followup>"),
    )
    plan = _one_step_plan()
    review = review_step("g", plan, plan.steps[0], "did it", [], PlanMemory(),
                         tools=Tools(tmp_path), models=CFG, client=object())
    assert review.verdict == "follow_up"
    assert review.followup == "add the missing docstring"


def test_review_revise_plan(monkeypatch, tmp_path):
    monkeypatch.setattr(
        planner_mod, "call_model",
        _FakeBrain("<verdict>revise_plan</verdict><reason>discovered a config we must honor</reason>"),
    )
    plan = _one_step_plan()
    review = review_step("g", plan, plan.steps[0], "did it", [], PlanMemory(),
                         tools=Tools(tmp_path), models=CFG, client=object())
    assert review.verdict == "revise_plan"
    assert "config" in review.reason


def test_review_empty_followup_downgrades_to_followup_with_hint(monkeypatch, tmp_path):
    """v0.0.32 (0.2): the v0.0.31 behavior here was a silent rubber-stamp --
    a ``<verdict>follow_up</verdict>`` with no followup instruction was
    downgraded to ``accept``, letting an un-actionable follow-up through.
    The new fail-CLOSED contract: a follow-up with no instruction is
    STILL a follow-up, but with a clear "say WHAT you want fixed" hint
    so the next review turn has something to act on.
    """
    monkeypatch.setattr(planner_mod, "call_model", _FakeBrain("<verdict>follow_up</verdict>"))
    plan = _one_step_plan()
    review = review_step("g", plan, plan.steps[0], "did it", [], PlanMemory(),
                         tools=Tools(tmp_path), models=CFG, client=object())
    assert review.verdict == "follow_up"
    assert "followup" in review.followup.lower()  # the hint names the problem


def test_review_unparseable_defaults_to_followup(monkeypatch, tmp_path):
    """v0.0.32 (0.2): the v0.0.31 behavior was a silent rubber-stamp on any
    unparseable review -- missing verdict, prose-only reply, ...
    defaulted to ``accept``. The new fail-CLOSED contract: an unparseable
    non-empty reply is a ``follow_up`` with a clear "verdict tag missing"
    hint. (An EMPTY reply -- the safe_default path from budget exhaustion
    -- still accepts, because we have nothing to act on.)
    """
    monkeypatch.setattr(planner_mod, "call_model", _FakeBrain("looks fine to me, nice work"))
    plan = _one_step_plan()
    review = review_step("g", plan, plan.steps[0], "did it", [], PlanMemory(),
                         tools=Tools(tmp_path), models=CFG, client=object())
    assert review.verdict == "follow_up"
    assert "verdict" in review.followup.lower()  # names what's missing


# --- v0.0.24: the AGENTIC reviewer reads the real work before verdicting --------


class _ScriptedBrain:
    """A brain returning queued replies in order (then repeating the last forever),
    so a multi-turn read-then-verdict investigation can be driven, network-free."""

    def __init__(self, *replies):
        self.queue = list(replies)
        self.last = replies[-1] if replies else "<verdict>accept</verdict>"
        self.calls = 0

    def __call__(self, role, messages, **kwargs):
        self.calls += 1
        if self.queue:
            self.last = self.queue.pop(0)
        return SimpleNamespace(text=self.last, record=None)


class _ContentAwareBrain:
    """Models the documented loop's FIX: blind (seeing only a byte-count), the old
    reviewer rejected; shown the file's REAL contents, this brain accepts. It reads
    until the correct-content marker appears in its context, then verdicts accept --
    so it can only reach 'accept by judgment' if the read actually delivered the file."""

    def __init__(self, marker):
        self.marker = marker
        self.calls = 0

    def __call__(self, role, messages, **kwargs):
        self.calls += 1
        joined = "\n".join(m["content"] for m in messages)
        if self.marker in joined:
            return SimpleNamespace(text="<verdict>accept</verdict>", record=None)
        return SimpleNamespace(text='<read path="impl.py"/>', record=None)


def test_reviewer_reads_touched_file_before_verdicting(monkeypatch, tmp_path):
    (tmp_path / "answer.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
    brain = _ScriptedBrain('<read path="answer.py"/>', "<verdict>accept</verdict>")
    monkeypatch.setattr(planner_mod, "call_model", brain)
    events = []
    plan = _one_step_plan()
    review = review_step(
        "g", plan, plan.steps[0], "wrote answer.py", [], PlanMemory(),
        tools=Tools(tmp_path), touched_paths=["answer.py"], models=CFG, client=object(),
        on_event=lambda k, m, p: events.append((k, m, p)),
    )
    assert review.verdict == "accept"
    assert brain.calls == 2  # it READ first, then verdicted -- not a one-shot blind call
    # The read hit the real touched file and its contents flowed back to the brain.
    reads = [p for k, m, p in events if k == "brain_action" and "answer.py" in m]
    assert reads and "return 42" in reads[0]["observation"]


def test_agentic_reviewer_accepts_correct_file_a_blind_reviewer_would_reject(monkeypatch, tmp_path):
    # The documented loop: the hands wrote a CORRECT file; a blind reviewer (seeing only
    # "wrote impl.py (N bytes)") would reject it. The agentic reviewer reads the real
    # file, sees it is correct, and ACCEPTS.
    (tmp_path / "impl.py").write_text("# CORRECT_IMPLEMENTATION\nvalue = 1\n", encoding="utf-8")
    plan = _one_step_plan()
    blind_transcript = ['[edit path="impl.py"]\nwrote impl.py (35 bytes, 2 lines)']  # no contents

    brain = _ContentAwareBrain("CORRECT_IMPLEMENTATION")
    monkeypatch.setattr(planner_mod, "call_model", brain)
    review = review_step(
        "g", plan, plan.steps[0], "wrote impl.py", blind_transcript, PlanMemory(),
        tools=Tools(tmp_path), touched_paths=["impl.py"], models=CFG, client=object(),
    )
    assert review.verdict == "accept"   # reading the real file turned a blind reject into accept
    assert brain.calls == 2             # read, then accept (the read is what enabled the judgment)

    # Contrast: with NO filesystem handle the same brain never sees the contents, so it
    # cannot reason its way to accept -- it burns its budget and falls back to the
    # safe default. v0.0.32 (0.2): the safe default is now ``follow_up`` (fail CLOSED)
    # rather than the v0.0.31 silent accept -- a stuck reviewer must NOT rubber-stamp.
    # The READ is what turns a follow-up into a genuine accept.
    brain_blind = _ContentAwareBrain("CORRECT_IMPLEMENTATION")
    monkeypatch.setattr(planner_mod, "call_model", brain_blind)
    review_blind = review_step(
        "g", plan, plan.steps[0], "wrote impl.py", blind_transcript, PlanMemory(),
        tools=None, touched_paths=["impl.py"], max_review_steps=3, models=CFG, client=object(),
    )
    assert review_blind.verdict == "follow_up"  # ...only via the (now fail-CLOSED) safe default
    assert brain_blind.calls == 3               # ...after exhausting the budget, never seeing the file


def test_reviewer_budget_exhaustion_fails_closed(monkeypatch, tmp_path):
    (tmp_path / "f.py").write_text("x = 1\n", encoding="utf-8")
    # A brain that investigates forever and never verdicts -> bounded by max_review_steps,
    # then the safe default fires. v0.0.32 (0.2): the safe default is now ``follow_up``
    # (fail CLOSED) -- a stuck reviewer that never verdicts must NOT rubber-stamp
    # accept. The follow-up text names the parse problem so the next turn has
    # something to act on; the run will exhaust the follow-up budget and the
    # step will fail, which is the right outcome (don't accept an unverified step).
    brain = _ScriptedBrain('<read path="f.py"/>')  # always reads, never a verdict
    monkeypatch.setattr(planner_mod, "call_model", brain)
    plan = _one_step_plan()
    review = review_step(
        "g", plan, plan.steps[0], "did it", [], PlanMemory(),
        tools=Tools(tmp_path), touched_paths=["f.py"], max_review_steps=3,
        models=CFG, client=object(),
    )
    assert review.verdict == "follow_up"  # was "accept" in v0.0.31
    assert "verdict" in review.followup.lower()  # names the parse problem
    assert brain.calls == 3  # exactly the review budget -- no infinite loop


def test_answer_self_answer(monkeypatch):
    monkeypatch.setattr(
        planner_mod, "call_model",
        _FakeBrain(
            "<decision>self_answer</decision><answer>reuse the existing db.py module</answer>"
            '<record kind="decision">reuse db.py :: reuse db</record>'
        ),
    )
    plan = _one_step_plan()
    res = answer_or_escalate("which db module?", "g", plan, plan.steps[0], PlanMemory(),
                             models=CFG, client=object())
    assert res.kind == "self_answer"
    assert res.answer == "reuse the existing db.py module"
    assert res.records == [("decision", "reuse db.py", "reuse db")]


def test_answer_escalate(monkeypatch):
    monkeypatch.setattr(
        planner_mod, "call_model",
        _FakeBrain("<decision>escalate</decision><ask_user>Should the app support OAuth login?</ask_user>"),
    )
    plan = _one_step_plan()
    res = answer_or_escalate("auth approach?", "g", plan, plan.steps[0], PlanMemory(),
                             models=CFG, client=object())
    assert res.kind == "escalate"
    assert res.question_for_user == "Should the app support OAuth login?"


def test_answer_unparseable_biases_to_escalate(monkeypatch):
    monkeypatch.setattr(planner_mod, "call_model", _FakeBrain("hmm, not totally sure about this"))
    plan = _one_step_plan()
    res = answer_or_escalate("the original question", "g", plan, plan.steps[0], PlanMemory(),
                             models=CFG, client=object())
    assert res.kind == "escalate"
    assert res.question_for_user == "the original question"  # falls back to the executor's question


def test_answer_self_answer_without_answer_tag_escalates(monkeypatch):
    monkeypatch.setattr(planner_mod, "call_model", _FakeBrain("<decision>self_answer</decision>"))
    plan = _one_step_plan()
    res = answer_or_escalate("q", "g", plan, plan.steps[0], PlanMemory(), models=CFG, client=object())
    assert res.kind == "escalate"  # claimed self-answer but gave none -> conservative escalate


def test_answer_reads_prior_decision_from_memory(monkeypatch):
    """Memory makes self-answers consistent: the prior decision reaches the brain."""
    mem = PlanMemory()
    mem.remember("decision", "storage backend is SQLite (chosen in step 1)", "chose SQLite",
                 provenance="step1", tags=["storage", "db"])
    captured = {}

    def fake(role, messages, **kwargs):
        captured["prompt"] = "\n".join(m["content"] for m in messages)
        return SimpleNamespace(text="<decision>self_answer</decision><answer>use SQLite</answer>", record=None)

    monkeypatch.setattr(planner_mod, "call_model", fake)
    plan = Plan.from_instructions(["build the storage layer"])
    res = answer_or_escalate("what storage backend should I use?", "build app", plan, plan.steps[0],
                             mem, models=CFG, client=object(), memory_budget_tokens=4000)
    assert "SQLite" in captured["prompt"]  # window-aware read delivered the prior decision
    assert res.kind == "self_answer" and "SQLite" in res.answer


def test_evolve_plan_returns_revised_tail(monkeypatch):
    monkeypatch.setattr(
        planner_mod, "call_model",
        _FakeBrain("<plan><step>new step A</step><step>new step B</step></plan>"),
    )
    plan = Plan.from_instructions(["done one", "old tail"])
    plan.mark_done(plan.steps[0], "did one")
    revised = evolve_plan("g", plan, "learned something", PlanMemory(), models=CFG, client=object())
    assert [s.instruction for s in revised.steps] == ["new step A", "new step B"]


def test_evolve_plan_abort_returns_none(monkeypatch):
    monkeypatch.setattr(planner_mod, "call_model", _FakeBrain("<abort>cannot continue</abort>"))
    revised = evolve_plan("g", Plan.from_instructions(["x"]), "r", PlanMemory(), models=CFG, client=object())
    assert revised is None


# --- v0.08: the assumption dial biases answer_or_escalate -------------------


class _DialHonoringBrain:
    """Simulates a model that obeys the dial: low dial -> self_answer (assume),
    high dial -> escalate (ask). Lets us prove the dial reaches the prompt AND
    changes the decision, network-free."""

    def __init__(self):
        self.prompts = []

    def __call__(self, role, messages, **kwargs):
        prompt = " ".join(m["content"] for m in messages)
        self.prompts.append(prompt)
        if "ASSUMPTION DIAL = 1" in prompt:
            text = "<decision>self_answer</decision><answer>assumed a sensible default</answer>"
        elif "ASSUMPTION DIAL = 5" in prompt:
            text = "<decision>escalate</decision><ask_user>which option do you want?</ask_user>"
        else:
            text = "<decision>self_answer</decision><answer>auto default</answer>"
        return SimpleNamespace(text=text, record=None)


def test_dial_threaded_into_answer_prompt(monkeypatch):
    brain = _DialHonoringBrain()
    monkeypatch.setattr(planner_mod, "call_model", brain)
    plan = _one_step_plan()
    answer_or_escalate("q?", "g", plan, plan.steps[0], PlanMemory(),
                       models=CFG, client=object(), assumption_level="5")
    assert "ASSUMPTION DIAL = 5" in brain.prompts[0]  # the dial reaches the prompt


def test_dial_biases_answer_decision(monkeypatch):
    monkeypatch.setattr(planner_mod, "call_model", _DialHonoringBrain())
    plan = _one_step_plan()

    low = answer_or_escalate("q?", "g", plan, plan.steps[0], PlanMemory(),
                             models=CFG, client=object(), assumption_level="1")
    high = answer_or_escalate("q?", "g", plan, plan.steps[0], PlanMemory(),
                              models=CFG, client=object(), assumption_level="5")

    assert low.kind == "self_answer"   # low dial -> the brain assumes
    assert high.kind == "escalate"     # high dial -> the brain asks the user

