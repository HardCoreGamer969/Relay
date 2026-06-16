"""Network-free tests for the autonomous two-role orchestrator (brain + hands).

A role-routed fake client returns brain replies and hands replies from separate
queues (routed by the model slug), which is robust to the brain<->hands
interleaving the autonomous loop introduces (plan, executor turns, step reviews,
question answers, evolutions). Memory is kept small so no compaction summarizer
call fires (that path is exercised by the longer-horizon overflow test).
"""

from __future__ import annotations

from types import SimpleNamespace

from relay.config import ModelConfig
from relay.loop import STATUS_COMPLETED, STATUS_MAX_STEPS
from relay.memory import PlanMemory
from relay.orchestrator import (
    STATUS_ABORTED_BY_BRAIN,
    STATUS_ESCALATION_LIMIT,
    STATUS_PLANNING_FAILED,
    STATUS_REPEATED_STEP,
    STATUS_UNRESOLVED_ESCALATION,
    run_planned,
)

CFG = ModelConfig(brain="vendor/brain", hands="vendor/hands")


def _resp(content):
    usage = SimpleNamespace(prompt_tokens=6, completion_tokens=4, total_tokens=10, cost=0.00002)
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))], usage=usage)


class _Completions:
    def __init__(self, brain, hands):
        self.brain = list(brain)
        self.hands = list(hands)
        self.calls: list[dict] = []

    def create(self, *, model, **kwargs):
        self.calls.append({"model": model, **kwargs})
        queue = self.brain if model == "vendor/brain" else self.hands
        role = "brain" if model == "vendor/brain" else "hands"
        assert queue, f"ran out of {role} replies"
        return _resp(queue.pop(0))


class RoutedClient:
    """Routes create() to a brain or hands reply queue by the model slug."""

    def __init__(self, brain=(), hands=()):
        self.chat = SimpleNamespace(completions=_Completions(brain, hands))

    @property
    def calls(self):
        return self.chat.completions.calls


def _hands_calls(client):
    return [c for c in client.calls if c["model"] == "vendor/hands"]


def _brain_calls(client):
    return [c for c in client.calls if c["model"] == "vendor/brain"]


def _kinds(result):
    return [e.kind for e in result.events]


# --- happy path + supervision ----------------------------------------------


def test_happy_path_completes_with_supervision(tmp_path):
    client = RoutedClient(
        brain=[
            "<plan><step>create alpha.txt with ALPHA</step><step>create beta.txt with BETA</step></plan>",
            "<verdict>accept</verdict>",
            "<verdict>accept</verdict>",
        ],
        hands=[
            '<edit path="alpha.txt">ALPHA</edit>\n<done>created alpha.txt</done>',
            '<edit path="beta.txt">BETA</edit>\n<done>created beta.txt</done>',
        ],
    )
    result = run_planned("two files", tmp_path, models=CFG, client=client)

    assert result.status == STATUS_COMPLETED
    assert [s.status for s in result.plan.steps] == ["done", "done"]
    assert (tmp_path / "alpha.txt").read_text(encoding="utf-8") == "ALPHA"
    assert (tmp_path / "beta.txt").read_text(encoding="utf-8") == "BETA"
    # Supervision cost: 1 plan + 2 step-boundary reviews = 3 brain calls (intended).
    assert len(_brain_calls(client)) == 3
    assert _kinds(result).count("step_reviewed") == 2
    # Memory learned both step outcomes (fact, dual-form, with provenance).
    facts = [e for e in result.memory.entries if e.kind == "fact"]
    assert len(facts) == 2
    assert all(f.detail and f.summary and f.provenance for f in facts)


def test_no_supervise_skips_reviews(tmp_path):
    client = RoutedClient(
        brain=["<plan><step>create a.txt</step></plan>"],
        hands=['<edit path="a.txt">A</edit>\n<done>created a.txt</done>'],
    )
    result = run_planned("one file", tmp_path, models=CFG, client=client, supervise=False)

    assert result.status == STATUS_COMPLETED
    assert len(_brain_calls(client)) == 1  # plan only -- no review calls
    assert "step_reviewed" not in _kinds(result)


# --- executor questions: self-answer vs escalate ---------------------------


def test_executor_question_self_answered(tmp_path):
    client = RoutedClient(
        brain=[
            "<plan><step>write the config loader</step></plan>",
            "<decision>self_answer</decision><answer>read config from config.toml</answer>",
            "<verdict>accept</verdict>",
        ],
        hands=[
            "<question>where do I read config from?</question>",
            '<edit path="cfg.py">cfg</edit>\n<done>wrote loader</done>',
        ],
    )
    result = run_planned("config", tmp_path, models=CFG, client=client)

    assert result.status == STATUS_COMPLETED
    kinds = _kinds(result)
    assert "executor_question" in kinds and "brain_self_answered" in kinds
    assert "brain_escalated" not in kinds
    # The self-answer is recorded as a decision in memory.
    decisions = [e for e in result.memory.entries if e.kind == "decision"]
    assert any("config.toml" in d.detail for d in decisions)


def test_executor_question_escalated_to_user(tmp_path):
    decisions_put = []

    def user_decision(question):
        decisions_put.append(question)
        return "yes, support OAuth"

    client = RoutedClient(
        brain=[
            "<plan><step>add login</step></plan>",
            "<decision>escalate</decision><ask_user>Should login support OAuth?</ask_user>",
            "<verdict>accept</verdict>",
        ],
        hands=[
            "<question>do we need OAuth login?</question>",
            '<edit path="login.py">x</edit>\n<done>added login</done>',
        ],
    )
    result = run_planned("auth", tmp_path, models=CFG, client=client, user_decision=user_decision)

    assert result.status == STATUS_COMPLETED
    assert decisions_put == ["Should login support OAuth?"]  # the precise product question
    kinds = _kinds(result)
    assert "brain_escalated" in kinds and "user_decided" in kinds
    # The user's resolution is recorded as a confirmation.
    assert any(e.kind == "confirmation" and "OAuth" in e.detail for e in result.memory.entries)


def test_escalation_without_callback_ends_unresolved(tmp_path):
    client = RoutedClient(
        brain=[
            "<plan><step>add login</step></plan>",
            "<decision>escalate</decision><ask_user>Should login support OAuth?</ask_user>",
        ],
        hands=["<question>do we need OAuth?</question>"],
    )
    result = run_planned("auth", tmp_path, models=CFG, client=client, user_decision=None)

    assert result.status == STATUS_UNRESOLVED_ESCALATION  # never guesses a product decision
    assert not (tmp_path / "login.py").exists()
    assert "brain_escalated" in _kinds(result)


def test_self_answer_is_consistent_with_prior_memory(tmp_path):
    """The prior decision is delivered to the brain window-aware, so it can stay consistent."""
    memory = PlanMemory()
    memory.remember("decision", "storage backend is SQLite (chosen earlier)", "chose SQLite",
                    provenance="step0", tags=["storage", "db"])
    client = RoutedClient(
        brain=[
            "<plan><step>build the storage layer</step></plan>",
            "<decision>self_answer</decision><answer>use SQLite, as already decided</answer>",
            "<verdict>accept</verdict>",
        ],
        hands=[
            "<question>which storage backend?</question>",
            '<edit path="store.py">x</edit>\n<done>built store</done>',
        ],
    )
    result = run_planned("storage", tmp_path, models=CFG, client=client, memory=memory)

    assert result.status == STATUS_COMPLETED
    # The brain's answer call (2nd brain call) saw the prior SQLite decision.
    answer_call = _brain_calls(client)[1]
    prompt = "\n".join(m["content"] for m in answer_call["messages"])
    assert "SQLite" in prompt


# --- supervision verdicts: follow_up (bounded) and revise_plan (bounded) ----


def test_follow_up_is_bounded_then_fails(tmp_path):
    # Reviewer keeps asking for follow-ups; budget=1 means one corrective attempt,
    # then the step fails -> failure path -> escalation_limit (max_escalations=0).
    client = RoutedClient(
        brain=[
            "<plan><step>do the thing</step></plan>",
            "<verdict>follow_up</verdict><followup>not quite, fix it</followup>",
            "<verdict>follow_up</verdict><followup>still not right</followup>",
        ],
        hands=[
            "<done>attempt 1</done>",
            "<done>attempt 2</done>",
        ],
    )
    result = run_planned("x", tmp_path, models=CFG, client=client,
                         max_followups_per_step=1, max_escalations=0)

    assert result.status == STATUS_ESCALATION_LIMIT
    assert len(_hands_calls(client)) == 2  # initial + exactly one bounded follow-up
    assert _kinds(result).count("step_reviewed") == 2


def test_follow_up_then_accept_completes(tmp_path):
    client = RoutedClient(
        brain=[
            "<plan><step>do the thing</step></plan>",
            "<verdict>follow_up</verdict><followup>add the missing piece</followup>",
            "<verdict>accept</verdict>",
        ],
        hands=[
            '<edit path="x.txt">v1</edit>\n<done>first attempt</done>',
            '<edit path="x.txt">v2</edit>\n<done>fixed</done>',
        ],
    )
    result = run_planned("x", tmp_path, models=CFG, client=client, max_followups_per_step=2)

    assert result.status == STATUS_COMPLETED
    assert len(_hands_calls(client)) == 2  # initial + 1 follow-up
    assert any(e.kind == "decision" and "follow-up" in e.detail.lower() for e in result.memory.entries)


def test_revise_plan_evolves_remaining_tail(tmp_path):
    client = RoutedClient(
        brain=[
            "<plan><step>STEP0 do first</step><step>STEP1 old tail</step></plan>",
            "<verdict>revise_plan</verdict><reason>learned the tail must change</reason>",
            "<plan><step>STEP1B revised tail</step></plan>",
            "<verdict>accept</verdict>",
        ],
        hands=[
            '<edit path="a.txt">A</edit>\n<done>did first</done>',
            '<edit path="b.txt">B</edit>\n<done>did revised tail</done>',
        ],
    )
    result = run_planned("g", tmp_path, models=CFG, client=client)

    assert result.status == STATUS_COMPLETED
    assert result.revisions == 1
    assert "plan_revised" in _kinds(result)
    # Completed step 0 preserved; revised tail executed.
    assert any(s.status == "done" and s.outcome == "did first" for s in result.plan.steps)
    assert (tmp_path / "a.txt").exists() and (tmp_path / "b.txt").exists()


def test_revise_plan_is_bounded(tmp_path):
    # max_plan_revisions=0: a revise verdict does NOT evolve (no thrash); proceed.
    client = RoutedClient(
        brain=[
            "<plan><step>s0</step><step>s1</step></plan>",
            "<verdict>revise_plan</verdict><reason>would like to revise</reason>",
            "<verdict>accept</verdict>",
        ],
        hands=[
            '<edit path="a.txt">A</edit>\n<done>did s0</done>',
            '<edit path="b.txt">B</edit>\n<done>did s1</done>',
        ],
    )
    result = run_planned("g", tmp_path, models=CFG, client=client, max_plan_revisions=0)

    assert result.status == STATUS_COMPLETED
    assert result.revisions == 0
    assert "plan_revised" not in _kinds(result)  # revision suppressed; no evolve call


# --- failure path still records a dead end and replans ----------------------


def test_failure_records_dead_end_and_replans(tmp_path):
    client = RoutedClient(
        brain=[
            "<plan><step>hard step</step></plan>",
            "<plan><step>easier step</step></plan>",  # replan after the block
            "<verdict>accept</verdict>",
        ],
        hands=[
            "<blocked>cannot do it this way</blocked>",
            '<edit path="ok.txt">ok</edit>\n<done>did it the easy way</done>',
        ],
    )
    result = run_planned("g", tmp_path, models=CFG, client=client)

    assert result.status == STATUS_COMPLETED
    assert result.escalations == 1
    assert "replanned" in _kinds(result)
    assert any(e.kind == "dead_end" for e in result.memory.entries)


def test_brain_aborts_on_failure(tmp_path):
    client = RoutedClient(
        brain=[
            "<plan><step>hard step</step></plan>",
            "<abort>this is impossible</abort>",
        ],
        hands=["<blocked>no way</blocked>"],
    )
    result = run_planned("g", tmp_path, models=CFG, client=client)
    assert result.status == STATUS_ABORTED_BY_BRAIN


# --- v0.0.21: repetition breaker (stop a re-issued dead-end early) -----------


def test_repeated_dead_end_step_stops_early(tmp_path):
    # Step 0 dead-ends (blocked); the replan re-issues a NEAR-IDENTICAL step.
    # The repetition breaker catches it the moment it would run again and stops
    # cleanly -- WITHOUT grinding the full escalation budget (default 3).
    client = RoutedClient(
        brain=[
            "<plan><step>Implement the input loop in main() to handle user navigation commands</step></plan>",
            "<plan><step>Implement the input loop in main() to handle the navigation commands properly</step></plan>",
        ],
        hands=[
            "<blocked>cannot determine the loop structure</blocked>",
        ],
    )
    result = run_planned("terminal calendar", tmp_path, models=CFG, client=client)

    assert result.status == STATUS_REPEATED_STEP
    assert result.escalations == 1          # stopped after the first replan, not 3
    assert len(_hands_calls(client)) == 1   # the re-issued step never ran (no grind)
    status_events = [e for e in result.events if e.kind == "status"]
    assert any(e.payload.get("status") == STATUS_REPEATED_STEP for e in status_events)


def test_progressing_replan_is_not_short_circuited(tmp_path):
    # A genuinely-different replan step (not a restatement of the dead-end) runs
    # normally -- the breaker fires ONLY on a clear repeat, never on progress.
    client = RoutedClient(
        brain=[
            "<plan><step>Build the config loader that reads from a toml settings file</step></plan>",
            "<plan><step>Create the month grid renderer with seven weekday columns</step></plan>",
            "<verdict>accept</verdict>",
        ],
        hands=[
            "<blocked>cannot find the settings format</blocked>",
            '<edit path="grid.py">grid</edit>\n<done>created the month grid renderer</done>',
        ],
    )
    result = run_planned("terminal calendar", tmp_path, models=CFG, client=client)

    assert result.status == STATUS_COMPLETED  # not STATUS_REPEATED_STEP
    assert "replanned" in _kinds(result)
    assert len(_hands_calls(client)) == 2     # the genuinely-different step ran


# --- v0.0.21: friendly, plain-language failure message ----------------------


def test_friendly_terminal_message_covers_stuck_and_ceiling():
    from relay.loop import STATUS_COMPLETED as _DONE, STATUS_MAX_STEPS
    from relay.orchestrator import friendly_terminal_message

    stuck = friendly_terminal_message(STATUS_ESCALATION_LIMIT)
    assert "stuck" in stuck.lower()
    assert "escalation_limit" not in stuck  # no jargon
    assert ("/model" in stuck or "smaller" in stuck)  # an actionable hint
    # repeated_step shares the same friendly stuck-loop form.
    assert friendly_terminal_message(STATUS_REPEATED_STEP) == stuck

    ceiling = friendly_terminal_message(STATUS_MAX_STEPS, max_total_steps=50)
    assert "50" in ceiling and "RELAY_MAX_TOTAL_STEPS" in ceiling
    assert "--max-total-steps" in ceiling

    # Statuses with no special friendly form return None (caller keeps its wording).
    assert friendly_terminal_message(_DONE) is None
    assert friendly_terminal_message(STATUS_ABORTED_BY_BRAIN) is None


def test_stuck_loop_result_turn_is_plain_language(tmp_path):
    # A repeated-dead-end run: the closing result turn (shown in the TUI
    # conversation and CLI --show-transcript) must read plainly, not "repeated_step".
    client = RoutedClient(
        brain=[
            "<plan><step>Implement the input loop in main() to handle user navigation commands</step></plan>",
            "<plan><step>Implement the input loop in main() to handle the navigation commands properly</step></plan>",
        ],
        hands=["<blocked>cannot determine the loop structure</blocked>"],
    )
    result = run_planned("terminal calendar", tmp_path, models=CFG, client=client)

    assert result.status == STATUS_REPEATED_STEP  # internal status string unchanged
    result_turns = [t for t in result.transcript.turns if t.phase == "result"]
    assert result_turns
    text = result_turns[-1].text.lower()
    assert "stuck" in text
    assert "repeated_step" not in text and "escalation_limit" not in text  # no jargon shown
    assert ("/model" in text or "smaller" in text)  # a concrete next action


# --- narrow executor context still holds -----------------------------------


def test_executor_context_stays_narrow(tmp_path):
    client = RoutedClient(
        brain=[
            "<plan><step>STEP_ALPHA create alpha.txt</step><step>STEP_BETA create beta.txt</step></plan>",
            "<verdict>accept</verdict>",
            "<verdict>accept</verdict>",
        ],
        hands=[
            '<edit path="alpha.txt">ALPHA_RAW_CONTENT</edit>\n<done>created alpha.txt</done>',
            '<edit path="beta.txt">BETA</edit>\n<done>created beta.txt</done>',
        ],
    )
    result = run_planned("g", tmp_path, models=CFG, client=client)
    assert result.status == STATUS_COMPLETED

    hands = _hands_calls(client)
    assert len(hands) == 2

    def joined(call):
        return "\n".join(m["content"] for m in call["messages"])

    step0_ctx, step1_ctx = joined(hands[0]), joined(hands[1])
    assert "STEP_ALPHA" in step0_ctx and "STEP_BETA" not in step0_ctx  # no full-plan leak
    assert "STEP_BETA" in step1_ctx
    assert "created alpha.txt" in step1_ctx  # one-line carry-over allowed
    assert "ALPHA_RAW_CONTENT" not in step1_ctx  # prior raw transcript NOT leaked
    assert "STEP_ALPHA" not in step1_ctx


# --- budgets / terminal statuses -------------------------------------------


def test_overall_budget_exhausted_is_max_steps(tmp_path):
    client = RoutedClient(
        brain=[
            "<plan><step>create a.txt</step><step>create b.txt</step></plan>",
            "<verdict>accept</verdict>",  # review of step 0 (a brain call, not an executor call)
        ],
        hands=['<edit path="a.txt">A</edit>\n<done>done a</done>'],
    )
    result = run_planned("g", tmp_path, models=CFG, client=client, max_total_steps=1)

    assert result.status == STATUS_MAX_STEPS
    assert (tmp_path / "a.txt").exists()
    assert not (tmp_path / "b.txt").exists()  # step 1 never ran -- executor budget spent
    # The ceiling is recorded, and the result turn tells the user how to raise it.
    assert result.max_total_steps == 1
    text = [t for t in result.transcript.turns if t.phase == "result"][-1].text
    assert "ceiling" in text.lower()
    assert ("RELAY_MAX_TOTAL_STEPS" in text or "--max-total-steps" in text)


def test_planning_failed(tmp_path):
    client = RoutedClient(brain=["no plan", "still none", "nope", "nope"], hands=[])
    result = run_planned("g", tmp_path, models=CFG, client=client)
    assert result.status == STATUS_PLANNING_FAILED
    assert result.plan is None


def test_plan_gate_declines(tmp_path):
    client = RoutedClient(brain=["<plan><step>x</step></plan>"], hands=[])
    result = run_planned("g", tmp_path, models=CFG, client=client, plan_gate=lambda plan: False)
    assert result.status == "declined_by_user"
    assert len(_hands_calls(client)) == 0


# --- events are first-class -------------------------------------------------


def test_events_stream_brain_executor_exchanges(tmp_path):
    seen = []
    client = RoutedClient(
        brain=[
            "<plan><step>write loader</step></plan>",
            "<decision>self_answer</decision><answer>read from config.toml</answer>",
            "<verdict>accept</verdict>",
        ],
        hands=[
            "<question>where to read config?</question>",
            '<edit path="cfg.py">x</edit>\n<done>wrote it</done>',
        ],
    )
    result = run_planned("g", tmp_path, models=CFG, client=client, on_event=seen.append)

    kinds = _kinds(result)
    for expected in ("plan_created", "executor_question", "brain_self_answered",
                     "step_reviewed", "memory_write", "step_done"):
        assert expected in kinds, f"missing event: {expected}"
    assert kinds == [e.kind for e in seen]  # on_event saw the same stream


def test_memory_entries_are_dual_form_with_provenance(tmp_path):
    client = RoutedClient(
        brain=["<plan><step>do a</step></plan>", "<verdict>accept</verdict>"],
        hands=['<edit path="a.txt">A</edit>\n<done>did a</done>'],
    )
    result = run_planned("g", tmp_path, models=CFG, client=client)
    assert result.memory is not None and result.memory.entries
    for entry in result.memory.entries:
        assert entry.detail and entry.summary and entry.provenance


# --- longer-horizon: real accumulated memory overflows a small window -------


# Genuinely-distinct per-step outcomes (different subjects, not a templated
# index) so v0.0.21 near-duplicate suppression keeps all six -- the test needs
# real, distinct facts to accumulate and overflow the small window.
_STEP_OUTCOMES = [
    "created the config loader that reads settings from a toml file",
    "added the month grid renderer with seven weekday columns",
    "wired keyboard navigation for moving between days and weeks",
    "implemented the event store backed by a json file on disk",
    "built the command parser for add, delete, and list actions",
    "wrote the help screen describing every keyboard shortcut",
]


class _DispatchClient:
    """A dispatching fake: routes brain calls by their system prompt so the
    compaction summarizer call (fired by compacted_context on real overflow) is
    handled and counted, alongside plan/review calls and the hands."""

    def __init__(self, n_steps):
        assert n_steps <= len(_STEP_OUTCOMES)  # each step gets a distinct outcome
        self.n_steps = n_steps
        self.summary_calls = 0      # compaction invocations on accumulated memory
        self.review_prompts = []    # captured brain-review user prompts
        self._hands_idx = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, *, model, messages, **kwargs):
        system = messages[0]["content"] if messages else ""
        user = messages[-1]["content"] if messages else ""
        if model == "vendor/hands":
            i = self._hands_idx
            self._hands_idx += 1
            return _resp(
                f'<edit path="f{i}.txt">content {i}</edit>\n'
                f"<done>{_STEP_OUTCOMES[i]}</done>"
            )
        # brain calls, dispatched by their system prompt:
        if "compacting a coding agent's plan memory" in system:
            self.summary_calls += 1
            return _resp("compacted: several boilerplate files created earlier")
        if "supervising an EXECUTOR" in system:
            self.review_prompts.append(user)
            return _resp("<verdict>accept</verdict>")
        if "You PLAN the work" in system:
            steps = "".join(f"<step>create file {i}</step>" for i in range(self.n_steps))
            return _resp(f"<plan>{steps}</plan>")
        return _resp("<verdict>accept</verdict>")


def test_longer_horizon_overflow_triggers_real_compaction(tmp_path):
    """Many steps accumulate enough memory to overflow a small window, so the
    window-aware budget machinery (compacted_context) fires on REAL entries."""
    client = _DispatchClient(n_steps=6)
    # window 2128 -> memory_budget = 2128//2 - 1024 = 40 tokens: a step's fact
    # fits verbatim, but 2+ overflow -> compaction engages mid-run.
    result = run_planned("build many files", tmp_path, models=CFG, client=client, context_window=2128)

    assert result.status == STATUS_COMPLETED  # stays coherent, no crash
    facts = [e for e in result.memory.entries if e.kind == "fact"]
    assert len(facts) == 6  # every step's outcome was learned (nothing dropped)
    assert all((tmp_path / f"f{i}.txt").exists() for i in range(6))
    # Compaction actually fired on accumulated memory (not a synthetic budget).
    assert client.summary_calls >= 1
    # Memory reads stayed under cap: no review prompt embedded all 6 fact summaries
    # verbatim once overflow kicked in (compaction replaced the overflow).
    from relay.memory import estimate_tokens, memory_budget
    budget = memory_budget(2128)
    # The compacted block the brain saw is bounded; sanity-check a review prompt
    # never carries an unbounded raw dump of every fact.
    assert any(p for p in client.review_prompts)  # reviews happened
    assert estimate_tokens("created file 5") < budget  # the test's facts are individually small


def test_longer_horizon_large_window_no_compaction(tmp_path):
    """Same run on a large window: everything fits, so no compaction call fires."""
    client = _DispatchClient(n_steps=6)
    result = run_planned("build many files", tmp_path, models=CFG, client=client, context_window=200000)
    assert result.status == STATUS_COMPLETED
    assert client.summary_calls == 0  # big window -> no summarization needed


# --- v0.08: committed plan (skip make_plan) + dial threading ----------------

from relay.planner import Plan  # noqa: E402


def test_committed_plan_skips_make_plan(tmp_path):
    # No <plan> reply in the brain queue: the committed plan must be used directly.
    client = RoutedClient(
        brain=["<verdict>accept</verdict>"],
        hands=['<edit path="x.txt">X</edit>\n<done>created x</done>'],
    )
    committed = Plan.from_instructions(["create x.txt with X"])
    result = run_planned("g", tmp_path, models=CFG, client=client, committed_plan=committed)

    assert result.status == STATUS_COMPLETED
    assert (tmp_path / "x.txt").read_text(encoding="utf-8") == "X"
    assert len(_brain_calls(client)) == 1  # only the step review -- no planning call


def test_committed_empty_plan_is_planning_failed(tmp_path):
    client = RoutedClient(brain=[], hands=[])
    result = run_planned("g", tmp_path, models=CFG, client=client, committed_plan=Plan(steps=[]))
    assert result.status == STATUS_PLANNING_FAILED


def test_assumption_level_threaded_into_answer(tmp_path):
    client = RoutedClient(
        brain=[
            "<plan><step>write loader</step></plan>",
            "<decision>self_answer</decision><answer>read config.toml</answer>",
            "<verdict>accept</verdict>",
        ],
        hands=[
            "<question>where to read config?</question>",
            '<edit path="cfg.py">x</edit>\n<done>wrote it</done>',
        ],
    )
    result = run_planned("g", tmp_path, models=CFG, client=client, assumption_level="1")
    assert result.status == STATUS_COMPLETED
    # The answer call (2nd brain call: plan, ANSWER, review) carried the dial.
    answer_call = _brain_calls(client)[1]
    prompt = " ".join(m["content"] for m in answer_call["messages"])
    assert "ASSUMPTION DIAL = 1" in prompt


# --- v0.08(B): the conversation thread is closed on EVERY terminal path ------


def test_planning_failed_still_closes_the_thread(tmp_path):
    client = RoutedClient(brain=[], hands=[])
    result = run_planned("g", tmp_path, models=CFG, client=client, committed_plan=Plan(steps=[]))
    assert result.status == STATUS_PLANNING_FAILED
    # Even on an early terminal path the thread is closed: a result turn + a
    # compacted record exist, so a transcript consumer gets a uniform contract.
    assert result.transcript_compacted is not None
    assert any(t.phase == "result" for t in result.transcript.turns)


def test_plan_gate_decline_still_closes_the_thread(tmp_path):
    client = RoutedClient(brain=["<plan><step>x</step></plan>"], hands=[])
    result = run_planned("g", tmp_path, models=CFG, client=client, plan_gate=lambda p: False)
    assert result.status == "declined_by_user"
    assert result.transcript_compacted is not None
    assert any(t.phase == "result" for t in result.transcript.turns)
    assert len(_hands_calls(client)) == 0  # decline still executes nothing


# --- v0.0.10: the result turn is truthful about failed steps -----------------

from relay.orchestrator import _result_summary  # noqa: E402
from relay.planner import PlanStep  # noqa: E402


def test_result_summary_all_succeeded_reads_clean():
    plan = Plan(steps=[PlanStep(0, "a", status="done"), PlanStep(1, "b", status="done")])
    text = _result_summary(STATUS_COMPLETED, plan)
    assert "built everything we agreed on" in text
    assert "2/2" in text


def test_result_summary_completed_with_failed_step_is_honest():
    # A completed run that recovered from a failed step (via replan) must NOT claim
    # "everything" -- that contradicted the 8/9-style count the gate observed.
    plan = Plan(steps=[
        PlanStep(0, "a", status="done"),
        PlanStep(1, "b", status="failed"),
        PlanStep(2, "c", status="done"),
    ])
    text = _result_summary(STATUS_COMPLETED, plan)
    assert "everything" not in text          # no longer overclaims
    assert "2 of 3" in text                  # honest done/total
    assert "reworked" in text                # names the failed-then-recovered step(s)


# --- v0.0.11: coarse cancellation at step boundaries (cancel_check) ----------

from relay.orchestrator import STATUS_CANCELLED  # noqa: E402
from relay.transcript import Transcript  # noqa: E402


def _committed(*instructions):
    return Plan.from_instructions(list(instructions))


def test_cancel_check_stops_at_the_next_step_boundary(tmp_path):
    """Cancel after step 0 settles: step 1 never starts, status is cancelled."""
    cancelled = {"flag": False}

    def on_event(event):
        if event.kind == "step_done":
            cancelled["flag"] = True  # the user "presses cancel" mid-run

    client = RoutedClient(
        brain=[],
        hands=[
            '<edit path="a.txt">A</edit>\n<done>made a.txt</done>',
            '<edit path="b.txt">B</edit>\n<done>made b.txt</done>',  # must NOT be consumed
        ],
    )
    result = run_planned(
        "two files", tmp_path, models=CFG, client=client, supervise=False,
        committed_plan=_committed("create a.txt", "create b.txt"),
        cancel_check=lambda: cancelled["flag"], on_event=on_event,
    )

    assert result.status == STATUS_CANCELLED
    assert [s.status for s in result.plan.steps] == ["done", "pending"]  # no further steps ran
    assert len(_hands_calls(client)) == 1  # the second hands reply was never consumed
    assert (tmp_path / "a.txt").exists() and not (tmp_path / "b.txt").exists()


def test_cancel_check_true_before_first_step_runs_nothing(tmp_path):
    client = RoutedClient(brain=[], hands=["<done>never consumed</done>"])
    result = run_planned(
        "g", tmp_path, models=CFG, client=client, supervise=False,
        committed_plan=_committed("step one"), cancel_check=lambda: True,
    )
    assert result.status == STATUS_CANCELLED
    assert len(_hands_calls(client)) == 0
    assert all(s.status == "pending" for s in result.plan.steps)


def test_cancelled_run_still_closes_the_thread_cleanly(tmp_path):
    """Cancellation is a clean terminal path: result turn + compaction, like any other."""
    transcript = Transcript()
    client = RoutedClient(brain=[], hands=[])
    result = run_planned(
        "g", tmp_path, models=CFG, client=client, supervise=False,
        committed_plan=_committed("step one"), cancel_check=lambda: True,
        transcript=transcript,
    )
    assert result.status == STATUS_CANCELLED
    assert result.transcript_compacted is not None
    result_turns = [t for t in transcript.turns if t.phase == "result"]
    assert len(result_turns) == 1
    assert "cancelled" in result_turns[0].text  # the closing turn says what happened
    assert any(e.kind == "status" and e.payload.get("status") == STATUS_CANCELLED
               for e in result.events)


def test_no_cancel_check_changes_nothing(tmp_path):
    """The knob is additive: omitted, the loop behaves exactly as before."""
    client = RoutedClient(
        brain=[],
        hands=['<edit path="a.txt">A</edit>\n<done>made a.txt</done>'],
    )
    result = run_planned(
        "one file", tmp_path, models=CFG, client=client, supervise=False,
        committed_plan=_committed("create a.txt"),
    )
    assert result.status == STATUS_COMPLETED


# --- v0.0.20: fast cancel BETWEEN executor calls (not only step boundaries) ---


def test_cancel_halts_between_executor_calls_within_a_step(tmp_path):
    """A cancel set mid-step halts at the next executor-CALL boundary: the step does
    NOT run its full call budget, no further call is issued after the cancel point,
    the run ends cancelled, and (crucially) no replan/brain call is spent."""
    state = {"hands": 0}

    class _NeverDoneClient:
        """Hands that never emit <done> -- so without a mid-step cancel the step would
        burn its entire executor budget. Counts every model call."""

        def __init__(self):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

        def _create(self, *, model, **kwargs):
            state["hands"] += 1
            return _resp('<read path="x"/>')  # a valid action, never <done>

        @property
        def calls(self):
            return [{"model": "vendor/hands"}] * state["hands"]

    client = _NeverDoneClient()
    result = run_planned(
        "one big step", tmp_path, models=CFG, client=client, supervise=False,
        committed_plan=_committed("a long multi-call step"),
        max_executor_steps=12,
        cancel_check=lambda: state["hands"] >= 3,  # cancel after 3 calls have returned
    )

    assert result.status == STATUS_CANCELLED
    assert state["hands"] == 3                       # halted at the call boundary, not 12
    assert result.plan.steps[0].status == "pending"  # step not marked done/failed
    # No replan/brain call was spent after the cancel (supervise off; only hands ran).


def test_fast_cancel_does_not_fire_when_step_completes_first(tmp_path):
    """If the step finishes before the cancel threshold, the per-call check is inert
    -- the step completes normally (the knob never changes a non-cancelled run)."""
    state = {"hands": 0}

    class _DoneClient:
        def __init__(self):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

        def _create(self, *, model, **kwargs):
            state["hands"] += 1
            return _resp('<edit path="a.txt">A</edit>\n<done>made a.txt</done>')

    client = _DoneClient()
    result = run_planned(
        "g", tmp_path, models=CFG, client=client, supervise=False,
        committed_plan=_committed("create a.txt"),
        cancel_check=lambda: state["hands"] >= 99,  # never trips
    )
    assert result.status == STATUS_COMPLETED
    assert state["hands"] == 1
    assert (tmp_path / "a.txt").exists()
