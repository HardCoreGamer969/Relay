"""Telemetry: the recording layer baked in from commit one.

Every model call records tokens, cost, and latency. This data is the backbone
of Relay's model-comparison features later, so it is not optional even now.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator


@dataclass
class CallRecord:
    """Telemetry for a single model call."""

    role: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_s: float
    # OpenRouter's actual per-generation cost. None when not returned — pricing
    # must never crash a run.
    cost_usd: float | None = None
    # Optional purpose tag (e.g. ``skeptic``) for cost attribution beyond role.
    purpose: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class RoleSummary:
    """Aggregated telemetry for all calls made under one role."""

    role: str
    model: str
    calls: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float | None
    latency_s: float

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class Ledger:
    """Collects :class:`CallRecord`s and aggregates them."""

    records: list[CallRecord] = field(default_factory=list)
    # Count of model messages that contained no valid action (a first-class
    # model-quality signal: parse-failure rate, surfaced in the summary output).
    parse_failures: int = 0

    def add(self, record: CallRecord) -> CallRecord:
        self.records.append(record)
        return record

    def record_parse_failure(self) -> int:
        """Increment and return the running parse-failure count."""
        self.parse_failures += 1
        return self.parse_failures

    def total_cost(self) -> float | None:
        """Sum of known costs, or None if no call reported a cost."""
        costs = [r.cost_usd for r in self.records if r.cost_usd is not None]
        return sum(costs) if costs else None

    def total_time(self) -> float:
        return sum(r.latency_s for r in self.records)

    def by_role(self) -> dict[str, RoleSummary]:
        """Aggregate calls/tokens/cost/time per role."""
        summaries: dict[str, RoleSummary] = {}
        for r in self.records:
            s = summaries.get(r.role)
            if s is None:
                summaries[r.role] = RoleSummary(
                    role=r.role,
                    model=r.model,
                    calls=1,
                    prompt_tokens=r.prompt_tokens,
                    completion_tokens=r.completion_tokens,
                    cost_usd=r.cost_usd,
                    latency_s=r.latency_s,
                )
            else:
                s.calls += 1
                s.prompt_tokens += r.prompt_tokens
                s.completion_tokens += r.completion_tokens
                s.latency_s += r.latency_s
                if r.cost_usd is not None:
                    s.cost_usd = (s.cost_usd or 0.0) + r.cost_usd
        return summaries


@dataclass
class _Elapsed:
    """Mutable holder so ``timer()`` can expose the measured duration."""

    seconds: float = 0.0


@contextmanager
def timer() -> Iterator[_Elapsed]:
    """Measure wall-clock latency of the wrapped block.

    Usage::

        with timer() as elapsed:
            do_work()
        print(elapsed.seconds)
    """
    elapsed = _Elapsed()
    start = time.perf_counter()
    try:
        yield elapsed
    finally:
        elapsed.seconds = time.perf_counter() - start
