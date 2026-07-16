"""Durable run records: persist each run so runs are comparable over time.

A run's per-role cost/token/time split prints to the console and then vanishes;
this module persists it as a structured :class:`RunRecord`, serialized as
**JSONL** (one JSON object per line -- append-only, trivially readable, no schema
migrations). The record carries everything the later run-matrix (v0.1) needs to
compare model pairings; ``schema_version`` lets future readers adapt.

This module deliberately does NOT compare or rank runs -- it only stores and
loads them. It is dependency-light (telemetry + config + stdlib) and duck-types
the run result so it never imports the loop/orchestrator.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from relay.config import ModelConfig
from relay.telemetry import Ledger

# Bump when the record shape changes incompatibly; readers branch on it.
SCHEMA_VERSION = 1

# Known RunRecord fields, used so load_records tolerates extra/missing keys.
_FIELDS = (
    "schema_version",
    "run_id",
    "timestamp",
    "goal",
    "mode",
    "roles",
    "status",
    "steps",
    "escalations",
    "parse_failures",
    "per_role",
    "totals",
    "wall_time_s",
    "envelope",
    "harness",
)


@dataclass
class RunRecord:
    """One persisted run, with everything needed to compare runs later.

    ``steps`` is the plan step count for ``mode="planned"`` and the number of
    executor turns for ``mode="solo"``. ``escalations`` is planned-only (0 for
    solo). ``per_role`` holds one entry per role (role, model, calls, tokens,
    cost_usd which may be ``None``, time_s); ``totals`` sums tokens/cost/time
    (cost only over known costs). ``wall_time_s`` is real wall-clock, distinct
    from the summed model latency in ``totals.time_s``.
    """

    schema_version: int
    run_id: str
    timestamp: str
    goal: str
    mode: str
    roles: dict[str, str]
    status: str
    steps: int
    escalations: int
    parse_failures: int
    per_role: list[dict] = field(default_factory=list)
    totals: dict = field(default_factory=dict)
    wall_time_s: float = 0.0
    # A1: optional envelope snapshot (ceilings, scope, wasted brain, outcome).
    envelope: dict | None = None
    # A2: optional harness flight-recorder snapshot (deterministic /why).
    harness: dict | None = None

    def to_json_line(self) -> str:
        """Serialize to a single JSONL line (ASCII-safe, trailing newline)."""
        return json.dumps(asdict(self), ensure_ascii=True) + "\n"

    @classmethod
    def from_dict(cls, data: dict) -> "RunRecord":
        """Build from a parsed JSON object, tolerating missing/extra keys."""
        known = {k: data[k] for k in _FIELDS if k in data}
        known.setdefault("schema_version", SCHEMA_VERSION)
        known.setdefault("run_id", "")
        known.setdefault("timestamp", "")
        known.setdefault("goal", "")
        known.setdefault("mode", "")
        known.setdefault("roles", {})
        known.setdefault("status", "")
        known.setdefault("steps", 0)
        known.setdefault("escalations", 0)
        known.setdefault("parse_failures", 0)
        known.setdefault("per_role", [])
        known.setdefault("totals", {})
        known.setdefault("wall_time_s", 0.0)
        return cls(**known)


def default_log_path(root: str | Path) -> Path:
    """The default run log location: ``<root>/.relay/runs.jsonl``."""
    return Path(root) / ".relay" / "runs.jsonl"


def build_record(
    *,
    goal: str,
    mode: str,
    result: Any,
    ledger: Ledger,
    models: ModelConfig,
    wall_time_s: float,
) -> RunRecord:
    """Assemble a :class:`RunRecord` from a finished run + its ledger and config.

    ``result`` is a ``PlannedTaskResult`` (planned) or ``TaskResult`` (solo);
    it is duck-typed. ``None`` costs are preserved (a JSON ``null``); totals sum
    only known costs.
    """
    summaries = ledger.by_role()

    per_role = [
        {
            "role": s.role,
            "model": s.model,
            "calls": s.calls,
            "prompt_tokens": s.prompt_tokens,
            "completion_tokens": s.completion_tokens,
            "total_tokens": s.total_tokens,
            "cost_usd": s.cost_usd,
            "time_s": round(s.latency_s, 4),
        }
        for s in summaries.values()
    ]

    totals = {
        "tokens": sum(r.total_tokens for r in ledger.records),
        "cost_usd": ledger.total_cost(),
        "time_s": round(ledger.total_time(), 4),
    }

    if mode == "planned":
        # The configured pairing (what the run-matrix groups on); for this
        # codebase the configured slug is exactly the one used.
        roles = {"brain": models.brain, "hands": models.hands}
        plan = getattr(result, "plan", None)
        steps = len(plan.steps) if plan is not None else 0
        escalations = int(getattr(result, "escalations", 0))
    else:  # solo: the single role that actually ran (no role param to rely on)
        roles = {s.role: s.model for s in summaries.values()}
        steps = len(getattr(result, "steps", []) or [])
        escalations = 0

    now = datetime.now(timezone.utc)
    envelope_snap = None
    env = getattr(result, "envelope", None)
    if env is not None:
        envelope_snap = {
            "max_cost": env.max_cost,
            "max_steps": env.max_steps,
            "scope": env.scope,
            "warn_thresholds": list(env.warn_thresholds),
            "wasted_brain_usd": env.wasted_brain_usd,
            "completed_steps": env.completed_steps,
            "outcome": env.outcome_label(getattr(result, "status", "")),
            "chargeable_cost": env.chargeable_cost(ledger),
        }
    return RunRecord(
        schema_version=SCHEMA_VERSION,
        run_id=now.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8],
        timestamp=now.isoformat(),
        goal=goal,
        mode=mode,
        roles=roles,
        status=getattr(result, "status", ""),
        steps=steps,
        escalations=escalations,
        parse_failures=ledger.parse_failures,
        per_role=per_role,
        totals=totals,
        wall_time_s=round(wall_time_s, 4),
        envelope=envelope_snap,
        harness=getattr(result, "harness", None),
    )


def append_record(record: RunRecord, path: str | Path) -> None:
    """Append one record as a JSONL line, creating the parent dir if missing."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(record.to_json_line())


def load_records(path: str | Path) -> list[RunRecord]:
    """Read all records from ``path``.

    A missing file returns ``[]`` (never raises); a malformed line is skipped
    rather than crashing the whole read.
    """
    path = Path(path)
    if not path.exists():
        return []
    records: list[RunRecord] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue  # tolerate a corrupt line; don't lose the rest of the log
        if isinstance(data, dict):
            records.append(RunRecord.from_dict(data))
    return records
