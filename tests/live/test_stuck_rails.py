"""Live: the safety rails fire with a real model — bounded, cheap, no spiral.

The README's central safety claim is that a weak/cheap model can't burn money in a
loop: every loop is bounded and a stuck run ends in ONE clear terminal status.
These tests induce "stuck" with a real model and assert it stops in a *known
bounded* status at *bounded* cost — the thing fakes can only approximate.
"""

from __future__ import annotations

import pytest

from relay.orchestrator import (
    STATUS_ABORTED_BY_BRAIN,
    STATUS_COMPLETED,
    STATUS_ESCALATION_LIMIT,
    STATUS_MAX_STEPS,
    STATUS_PLANNING_FAILED,
    STATUS_REPEATED_STEP,
    STATUS_UNRESOLVED_ESCALATION,
    run_planned,
)

pytestmark = pytest.mark.live

# Every non-success way a bounded run is allowed to end. The point is that the run
# stops in one of these, never hangs and never spirals past its budget.
BOUNDED_STOPS = frozenset(
    {
        STATUS_MAX_STEPS,
        STATUS_REPEATED_STEP,
        STATUS_ESCALATION_LIMIT,
        STATUS_ABORTED_BY_BRAIN,
        STATUS_PLANNING_FAILED,
        STATUS_UNRESOLVED_ESCALATION,
    }
)


def test_tiny_budget_stops_bounded_and_cheap(tmp_path, live_models, live_budget):
    # A multi-part task with a step budget far too small to finish: the rails must
    # stop it in a bounded status rather than grind.
    res = run_planned(
        "Build a small multi-module Python package with a CLI, a tests/ dir, and a README.",
        project_root=tmp_path,
        models=live_models,
        assumption_level="1",
        supervise=True,
        max_total_steps=2,  # deliberately too few to complete the work
        max_executor_steps=3,
        max_escalations=1,
        max_plan_revisions=1,
    )
    cost = res.ledger.total_cost()
    live_budget.charge(cost)

    assert res.status in BOUNDED_STOPS, (
        f"expected a bounded stop, got {res.status!r} "
        f"(events={[e.kind for e in res.events]})"
    )
    assert (cost or 0.0) < 0.10, f"runaway cost on a bounded run: ${cost}"


def test_ambiguous_goal_without_a_decision_seam_does_not_guess(
    tmp_path, live_models, live_budget
):
    # An underspecified goal, at the strictest assumption level, with NO
    # user_decision seam. Relay must stop rather than silently guess and build the
    # wrong thing ("a wrong self-answer silently builds the wrong thing").
    res = run_planned(
        "Add user authentication to the app.",  # which app? what kind? — undetermined
        project_root=tmp_path,
        models=live_models,
        assumption_level="5",  # exact-letter: assume almost nothing
        user_decision=None,  # no seam to escalate to
        supervise=True,
        max_total_steps=6,
        max_escalations=2,
    )
    cost = res.ledger.total_cost()
    live_budget.charge(cost)

    assert res.status != STATUS_COMPLETED, (
        "claimed a clean completion of an underspecified goal — it guessed"
    )
    assert res.status in BOUNDED_STOPS, f"unexpected terminal status {res.status!r}"
    assert (cost or 0.0) < 0.10, f"runaway cost on an unresolved run: ${cost}"
