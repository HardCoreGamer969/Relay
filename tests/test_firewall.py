"""Tests for the product-decision firewall (B2)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import relay.planner as planner_mod
from relay.config import ModelConfig
from relay.firewall import (
    DEFAULT_QUESTION_CLASS,
    classify_question,
    may_auto_answer,
    normalize_question_class,
)
from relay.memory import PlanMemory
from relay.planner import Plan, answer_or_escalate
from relay.protocol import parse

CFG = ModelConfig(brain="b", hands="h")


def _plan():
    return Plan.from_instructions(["do the thing"])


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, "product"),
        ("", "product"),
        ("TECH", "tech"),
        ("mechanical", "mechanical"),
        ("nope", "product"),
    ],
)
def test_normalize_fail_closed(raw, expected):
    assert normalize_question_class(raw) == expected


def test_classify_from_attribute_and_prefix():
    assert classify_question("which db?", explicit="tech") == ("tech", "which db?")
    assert classify_question("[mechanical] fix the import")[0] == "mechanical"
    assert classify_question("class: product\nShip dark mode?")[0] == "product"
    assert classify_question("unlabeled?")[0] == DEFAULT_QUESTION_CLASS


@pytest.mark.parametrize(
    "qclass,dial,auto",
    [
        ("product", "1", False),
        ("product", "2", False),
        ("product", "auto", False),
        ("product", "5", False),
        ("tech", "1", True),
        ("tech", "2", True),
        ("tech", "3", False),
        ("tech", "auto", False),
        ("tech", "5", False),
        ("mechanical", "1", True),
        ("mechanical", "3", True),
        ("mechanical", "auto", True),
        ("mechanical", "5", False),
    ],
)
def test_dial_class_matrix(qclass, dial, auto):
    assert may_auto_answer(qclass, dial) is auto


def test_protocol_parses_question_class():
    result = parse('<question class="tech">which library?</question>')
    q = result.first("question")
    assert q is not None
    assert q.question_class == "tech"
    assert q.content == "which library?"


def test_product_never_calls_model(monkeypatch):
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise AssertionError("product must not call the model")

    monkeypatch.setattr(planner_mod, "call_model", boom)
    plan = _plan()
    res = answer_or_escalate(
        "Ship dark mode?", "g", plan, plan.steps[0], PlanMemory(),
        models=CFG, client=object(), assumption_level="1", question_class="product",
    )
    assert res.kind == "escalate"
    assert res.question_class == "product"
    assert calls == []


def test_unlabeled_fails_closed_as_product(monkeypatch):
    monkeypatch.setattr(
        planner_mod, "call_model",
        lambda *a, **k: SimpleNamespace(
            text="<decision>self_answer</decision><answer>nope</answer>", record=None
        ),
    )
    plan = _plan()
    res = answer_or_escalate(
        "what should the button say?", "g", plan, plan.steps[0], PlanMemory(),
        models=CFG, client=object(), assumption_level="1",
    )
    assert res.kind == "escalate"
    assert res.question_class == "product"


def test_tech_auto_at_permissive_dial(monkeypatch):
    monkeypatch.setattr(
        planner_mod, "call_model",
        lambda *a, **k: SimpleNamespace(
            text="<decision>self_answer</decision><answer>use sqlite</answer>", record=None
        ),
    )
    plan = _plan()
    res = answer_or_escalate(
        "which db?", "g", plan, plan.steps[0], PlanMemory(),
        models=CFG, client=object(), assumption_level="1", question_class="tech",
    )
    assert res.kind == "self_answer"
    assert res.question_class == "tech"


def test_tech_escalates_at_cautious_dial(monkeypatch):
    calls = []

    def track(*a, **k):
        calls.append(1)
        return SimpleNamespace(text="<decision>self_answer</decision><answer>x</answer>", record=None)

    monkeypatch.setattr(planner_mod, "call_model", track)
    plan = _plan()
    res = answer_or_escalate(
        "which db?", "g", plan, plan.steps[0], PlanMemory(),
        models=CFG, client=object(), assumption_level="3", question_class="tech",
    )
    assert res.kind == "escalate"
    assert calls == []


def test_mechanical_usually_auto(monkeypatch):
    monkeypatch.setattr(
        planner_mod, "call_model",
        lambda *a, **k: SimpleNamespace(
            text="<decision>self_answer</decision><answer>sort imports</answer>", record=None
        ),
    )
    plan = _plan()
    res = answer_or_escalate(
        "should I sort imports?", "g", plan, plan.steps[0], PlanMemory(),
        models=CFG, client=object(), assumption_level="auto", question_class="mechanical",
    )
    assert res.kind == "self_answer"
