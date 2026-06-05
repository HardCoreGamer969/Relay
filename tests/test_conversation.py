"""Network-free tests for conversational planning (relay/conversation.py).

The brain (`call_model`) is dispatched by system prompt (scope vs propose), and
`user_turn` is scripted. No network.
"""

from __future__ import annotations

from types import SimpleNamespace

import relay.conversation as conv
from relay.config import ModelConfig
from relay.conversation import (
    Plan,
    _derive_plain,
    _is_commit,
    _posture,
    plan_conversationally,
)

CFG = ModelConfig(brain="vendor/brain", hands="vendor/hands")


def _resp(text):
    return SimpleNamespace(text=text, record=None)


class _ConvBrain:
    """Dispatches by system prompt: scope-assessment vs propose. Records prompts."""

    def __init__(self, scope_reply, propose_replies):
        self.scope_reply = scope_reply
        self._propose = list(propose_replies)
        self._last_propose = propose_replies[-1] if propose_replies else "<plan><step>do it</step></plan>"
        self.prompts = []
        self.scope_calls = 0
        self.propose_calls = 0

    def __call__(self, role, messages, **kwargs):
        # Flatten whitespace so dispatch markers match across prompt line wraps.
        system = " ".join(messages[0]["content"].split())
        self.prompts.append(" ".join(m["content"] for m in messages))
        if "assess the goal's SCOPE" in system:
            self.scope_calls += 1
            return _resp(self.scope_reply)
        if "precise, executor-ready plan" in system:
            self.propose_calls += 1
            reply = self._propose.pop(0) if self._propose else self._last_propose
            return _resp(reply)
        return _resp("")


def _scripted_user(*replies):
    queue = list(replies)

    def turn(prompt):
        return queue.pop(0) if queue else "ok"

    return turn


def _install(monkeypatch, scope_reply, propose_replies):
    brain = _ConvBrain(scope_reply, propose_replies)
    monkeypatch.setattr(conv, "call_model", brain)
    return brain


# --- pure helpers -----------------------------------------------------------


def test_posture_table_dial_modulation():
    assert _posture("small", "1") == "propose_fast"   # low dial: propose fast even...
    assert _posture("large", "1") == "propose_fast"   # ...on a large goal
    assert _posture("small", "5") == "elicit_first"   # high dial: elicit even on small
    assert _posture("small", "auto") == "propose_fast"
    assert _posture("large", "auto") == "elicit_first"
    assert _posture("ambiguous", "auto") == "scoping_question"


def test_is_commit():
    assert _is_commit("ok") and _is_commit("looks good") and _is_commit("LGTM.") and _is_commit("")
    assert not _is_commit("add dark mode")
    assert not _is_commit("yes but drop the login")


# --- scope posture ----------------------------------------------------------


def test_small_goal_proposes_fast(monkeypatch):
    _install(monkeypatch, "<scope>small</scope><reason>self-contained</reason>",
             ["<plan><step>create game.py</step></plan>"])
    result = plan_conversationally("a python snake game", ".", models=CFG, client=object(),
                                   user_turn=_scripted_user("ok"))
    assert result.posture == "propose_fast"
    assert result.questions_asked == 0  # propose-fast asks nothing
    assert result.committed is True


def test_large_goal_elicits_first(monkeypatch):
    _install(monkeypatch,
             "<scope>large</scope><reason>forking</reason><ask>Web or native?</ask><ask>Auth needed?</ask>",
             ["<plan><step>scaffold the app</step></plan>"])
    result = plan_conversationally("a full task-tracking app", ".", models=CFG, client=object(),
                                   user_turn=_scripted_user("web", "yes", "ok"))
    assert result.posture == "elicit_first"
    assert result.questions_asked == 2
    assert result.committed is True


def test_ambiguous_goal_asks_one_scoping_question(monkeypatch):
    _install(monkeypatch,
             "<scope>ambiguous</scope><reason>could be many things</reason><ask>Landing page or store?</ask>",
             ["<plan><step>build the landing page</step></plan>"])
    result = plan_conversationally("build me a website", ".", models=CFG, client=object(),
                                   user_turn=_scripted_user("landing page", "ok"))
    assert result.posture == "scoping_question"
    assert result.questions_asked == 1
    assert result.committed is True


def test_scope_assessment_is_logged(monkeypatch):
    _install(monkeypatch, "<scope>small</scope><reason>tiny</reason>",
             ["<plan><step>x</step></plan>"])
    result = plan_conversationally("g", ".", models=CFG, client=object(), user_turn=_scripted_user("ok"))
    scope_events = [e for e in result.events if e["kind"] == "scope_assessed"]
    assert scope_events and scope_events[0]["payload"]["scope"] == "small"


# --- conversation loop ------------------------------------------------------


def test_reaction_revises_then_commits(monkeypatch):
    _install(monkeypatch, "<scope>small</scope><reason>x</reason>",
             ["<plan><step>build a light-themed page</step></plan>",
              "<plan><step>build a dark-themed page</step></plan>"])
    result = plan_conversationally("a page", ".", models=CFG, client=object(),
                                   user_turn=_scripted_user("add dark mode", "looks good"))
    assert result.committed is True
    assert result.rounds == 2
    assert any("dark" in s.instruction for s in result.plan.steps)  # the reaction was folded in
    kinds = [e["kind"] for e in result.events]
    assert "user_reacted" in kinds and "plan_revised" in kinds


def test_round_cap_halts_non_committing_user(monkeypatch):
    _install(monkeypatch, "<scope>small</scope><reason>x</reason>",
             ["<plan><step>v1</step></plan>", "<plan><step>v2</step></plan>"])
    result = plan_conversationally("g", ".", models=CFG, client=object(),
                                   user_turn=_scripted_user("hmm", "still not sure", "nope"),
                                   max_rounds=2)
    assert result.committed is False
    assert result.rounds == 2
    assert any(e["kind"] == "not_committed" for e in result.events)


# --- the dial biases the conversation (the headline, conversation side) -----


def test_dial_biases_conversation_questions(monkeypatch):
    scope = "<scope>small</scope><reason>x</reason><ask>A low-stakes detail?</ask>"
    propose = ["<plan><step>do it</step></plan>"]

    _install(monkeypatch, scope, list(propose))
    low = plan_conversationally("g", ".", models=CFG, client=object(),
                                user_turn=_scripted_user("ok"), assumption_level="1")

    _install(monkeypatch, scope, list(propose))
    high = plan_conversationally("g", ".", models=CFG, client=object(),
                                 user_turn=_scripted_user("the answer", "ok"), assumption_level="5")

    assert low.questions_asked == 0     # level 1 assumes -> asks nothing
    assert high.questions_asked >= 1    # level 5 elicits even on a small goal


def test_level1_does_not_ask_what_level4_would(monkeypatch):
    scope = "<scope>small</scope><reason>x</reason><ask>A low-stakes detail?</ask>"
    propose = ["<plan><step>do it</step></plan>"]

    _install(monkeypatch, scope, list(propose))
    lvl1 = plan_conversationally("g", ".", models=CFG, client=object(),
                                 user_turn=_scripted_user("ok"), assumption_level="1")

    _install(monkeypatch, scope, list(propose))
    lvl4 = plan_conversationally("g", ".", models=CFG, client=object(),
                                 user_turn=_scripted_user("the answer", "ok"), assumption_level="4")

    assert lvl1.questions_asked == 0    # honors "assume freely"
    assert lvl4.questions_asked >= 1    # the same low-stakes detail IS asked at a higher dial


# --- dual-fidelity: derived, not parallel -----------------------------------


def test_plain_plan_is_derived_from_precise_steps(monkeypatch):
    _install(monkeypatch, "<scope>small</scope><reason>x</reason>",
             ["<plan><step>Add Google sign-in only</step><step>Store sessions in a cookie</step></plan>"
              "<assume>Login = Google sign-in only, no passwords</assume>"])
    result = plan_conversationally("an app with simple login", ".", models=CFG, client=object(),
                                   user_turn=_scripted_user("ok"))

    # Every precise step appears in the plain rendering (derived from the same steps).
    for step in result.plan.steps:
        assert step.instruction in result.plain_plan
    # The consequential addition is surfaced for confirmation.
    assert "Google sign-in only" in result.plain_plan
    assert result.assumptions == ["Login = Google sign-in only, no passwords"]
    # And the plain text is recomputable from the plan + assumptions (single source).
    assert result.plain_plan == _derive_plain(result.plan, result.assumptions)


def test_plain_and_spec_cannot_silently_diverge():
    # _derive_plain is a pure function of the plan; different steps -> different plain.
    p1 = Plan.from_instructions(["alpha step"])
    p2 = Plan.from_instructions(["beta step"])
    assert _derive_plain(p1, []) != _derive_plain(p2, [])
    assert "alpha step" in _derive_plain(p1, [])
    assert "beta step" not in _derive_plain(p1, [])


# --- level 5 surfaces contradictions (instruction threaded) -----------------


def test_level5_threads_contradiction_directive(monkeypatch):
    brain = _install(monkeypatch, "<scope>small</scope><reason>x</reason>",
                     ["<plan><step>do it</step></plan>"])
    plan_conversationally("add a python library to this JS project", ".", models=CFG, client=object(),
                          user_turn=_scripted_user("ok"), assumption_level="5")
    joined = " ".join(brain.prompts)
    assert "surface genuine impossibilities or contradictions" in joined  # level-5 directive present


def test_low_dial_omits_contradiction_directive(monkeypatch):
    brain = _install(monkeypatch, "<scope>small</scope><reason>x</reason>",
                     ["<plan><step>do it</step></plan>"])
    plan_conversationally("g", ".", models=CFG, client=object(),
                          user_turn=_scripted_user("ok"), assumption_level="1")
    joined = " ".join(brain.prompts)
    assert "surface genuine impossibilities or contradictions" not in joined
