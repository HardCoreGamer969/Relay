"""Network-free tests for the durable run log (relay/runlog.py)."""

from __future__ import annotations

from types import SimpleNamespace

from relay.config import ModelConfig
from relay.runlog import (
    SCHEMA_VERSION,
    RunRecord,
    append_record,
    build_record,
    default_log_path,
    load_records,
)
from relay.telemetry import CallRecord, Ledger


def _sample_record(run_id="r1", cost=0.0012):
    return RunRecord(
        schema_version=SCHEMA_VERSION,
        run_id=run_id,
        timestamp="2026-06-02T14:41:07+00:00",
        goal="make two files",
        mode="planned",
        roles={"brain": "vendor/brain", "hands": "vendor/hands"},
        status="completed",
        steps=2,
        escalations=0,
        parse_failures=0,
        per_role=[
            {"role": "brain", "model": "vendor/brain", "calls": 1, "prompt_tokens": 10,
             "completion_tokens": 5, "total_tokens": 15, "cost_usd": cost, "time_s": 0.1},
        ],
        totals={"tokens": 15, "cost_usd": cost, "time_s": 0.1},
        wall_time_s=1.25,
    )


def test_record_round_trips_through_json():
    record = _sample_record()
    line = record.to_json_line()
    assert line.endswith("\n")
    # One JSON object per line.
    assert line.count("\n") == 1
    restored = RunRecord.from_dict(__import__("json").loads(line))
    assert restored == record


def test_null_cost_round_trips_as_none():
    record = _sample_record(cost=None)
    record.totals["cost_usd"] = None
    record.per_role[0]["cost_usd"] = None
    restored = RunRecord.from_dict(__import__("json").loads(record.to_json_line()))
    assert restored.totals["cost_usd"] is None
    assert restored.per_role[0]["cost_usd"] is None
    assert restored == record


def test_build_record_planned_aggregates_per_role_and_totals():
    ledger = Ledger()
    ledger.add(CallRecord("brain", "vendor/brain", 100, 40, 1.0, 0.002))
    ledger.add(CallRecord("hands", "vendor/hands", 200, 60, 2.0, 0.001))
    ledger.add(CallRecord("hands", "vendor/hands", 50, 10, 0.5, 0.0005))
    result = SimpleNamespace(status="completed", plan=SimpleNamespace(steps=[1, 2, 3]), escalations=1)

    rec = build_record(
        goal="g", mode="planned", result=result, ledger=ledger,
        models=ModelConfig(brain="vendor/brain", hands="vendor/hands"), wall_time_s=9.0,
    )

    assert rec.mode == "planned"
    assert rec.roles == {"brain": "vendor/brain", "hands": "vendor/hands"}
    assert rec.status == "completed"
    assert rec.steps == 3
    assert rec.escalations == 1
    by_role = {entry["role"]: entry for entry in rec.per_role}
    assert by_role["hands"]["calls"] == 2
    assert by_role["hands"]["total_tokens"] == 320  # (200+60)+(50+10)
    assert abs(by_role["hands"]["cost_usd"] - 0.0015) < 1e-9
    assert rec.totals["tokens"] == 460
    assert abs(rec.totals["cost_usd"] - 0.0035) < 1e-9
    assert rec.wall_time_s == 9.0
    assert rec.run_id and rec.timestamp  # generated


def test_build_record_solo_shape():
    ledger = Ledger()
    ledger.add(CallRecord("hands", "vendor/hands", 30, 10, 1.0, None))
    result = SimpleNamespace(status="completed", steps=[1, 2, 3, 4])

    rec = build_record(
        goal="g", mode="solo", result=result, ledger=ledger,
        models=ModelConfig(brain="vendor/brain", hands="vendor/hands"), wall_time_s=2.0,
    )

    assert rec.mode == "solo"
    assert rec.roles == {"hands": "vendor/hands"}  # only the role that ran
    assert rec.steps == 4  # executor turns
    assert rec.escalations == 0
    assert rec.totals["cost_usd"] is None  # no known costs -> null


def test_append_and_load_round_trip(tmp_path):
    path = tmp_path / ".relay" / "runs.jsonl"
    append_record(_sample_record(run_id="first"), path)
    append_record(_sample_record(run_id="second"), path)

    loaded = load_records(path)
    assert [r.run_id for r in loaded] == ["first", "second"]  # in append order


def test_load_missing_file_returns_empty(tmp_path):
    assert load_records(tmp_path / "nope" / "runs.jsonl") == []


def test_load_skips_malformed_line(tmp_path):
    path = tmp_path / "runs.jsonl"
    path.write_text(
        _sample_record(run_id="good1").to_json_line()
        + "{ this is not valid json\n"
        + _sample_record(run_id="good2").to_json_line(),
        encoding="utf-8",
    )
    loaded = load_records(path)
    assert [r.run_id for r in loaded] == ["good1", "good2"]  # bad line skipped, rest intact


def test_default_log_path():
    assert default_log_path("/proj").as_posix().endswith(".relay/runs.jsonl")
