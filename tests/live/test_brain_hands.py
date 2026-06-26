"""Live: the core promise — the brain plans, the hands build, end to end.

This is the single most valuable live test: with a real cheap model it proves the
whole relay (plan -> step -> executor edit) actually works, that BOTH roles are
exercised (not a silent solo run), and that cost telemetry is extracted live.
Network-free fakes can model the protocol but can't prove a real model drives it.
"""

from __future__ import annotations

import pytest

from relay.orchestrator import STATUS_COMPLETED, run_planned

pytestmark = pytest.mark.live


def test_brain_relays_to_hands_and_builds_a_file(tmp_path, live_models, live_budget):
    res = run_planned(
        "Create a file named hello.py whose only line is: print('hello relay')",
        project_root=tmp_path,
        models=live_models,
        assumption_level="1",  # don't stall asking questions in a headless run
        supervise=True,
        max_total_steps=10,
        max_escalations=1,
    )

    # Charge the budget FIRST so the session cap is enforced even if asserts fail.
    cost = res.ledger.total_cost()
    live_budget.charge(cost)

    assert res.status == STATUS_COMPLETED, (
        f"run did not complete: status={res.status!r}; "
        f"events={[e.kind for e in res.events]}"
    )

    target = tmp_path / "hello.py"
    assert target.exists(), "the hands never created hello.py"
    assert "hello relay" in target.read_text(encoding="utf-8").lower()

    # The relay actually happened: both roles were called, not just one.
    roles = res.ledger.by_role()
    assert "brain" in roles, "brain was never called — no planning happened"
    assert "hands" in roles, "hands were never called — no execution happened"

    # Cost telemetry works live (DeepSeek's cache hit/miss split is extracted),
    # and a trivial task stays cheap.
    assert cost is not None and cost > 0, f"no cost recorded: {cost!r}"
    assert cost < 0.05, f"a one-file task cost ${cost:.4f} — unexpectedly expensive"
