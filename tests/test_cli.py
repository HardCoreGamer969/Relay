"""Network-free tests for v0.05 CLI surfaces: doctor, runs, run-persistence.

The OpenRouter client and the run engines are mocked, so no network is used.
"""

from __future__ import annotations

from types import SimpleNamespace

from typer.testing import CliRunner

from relay import cli
from relay.cli import _run_doctor, _runs_table, app
from relay.config import ModelConfig
from relay.runlog import RunRecord, append_record, default_log_path, load_records

runner = CliRunner()


# --- doctor helpers (no network) -------------------------------------------


class _DoctorCompletions:
    def __init__(self, good):
        self.good = set(good)

    def create(self, *, model, **kwargs):
        if model in self.good:
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])
        raise RuntimeError(f"Error code: 404 - No endpoints found for {model}")


class FakeDoctorClient:
    def __init__(self, good):
        self.chat = SimpleNamespace(completions=_DoctorCompletions(good))


def test_run_doctor_classifies_ok_and_failed():
    client = FakeDoctorClient(good={"vendor/ok"})
    rows, all_ok = _run_doctor([("brain", "vendor/ok"), ("hands", "vendor/bad")], client)

    assert rows[0]["status"] == "OK"
    assert rows[1]["status"] == "FAILED"
    assert "No endpoints" in rows[1]["note"]
    assert all_ok is False


def test_run_doctor_all_ok():
    client = FakeDoctorClient(good={"a", "b"})
    rows, all_ok = _run_doctor([("brain", "a"), ("hands", "b")], client)
    assert all_ok is True
    assert all(r["status"] == "OK" for r in rows)


def test_doctor_command_exits_nonzero_on_failure(monkeypatch):
    monkeypatch.setattr(cli, "load_models", lambda: ModelConfig(brain="vendor/ok", hands="vendor/bad"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(cli, "build_client", lambda: FakeDoctorClient(good={"vendor/ok"}))

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code != 0
    assert "FAILED" in result.output


def test_doctor_command_exits_zero_when_all_ok(monkeypatch):
    monkeypatch.setattr(cli, "load_models", lambda: ModelConfig(brain="vendor/a", hands="vendor/b"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(cli, "build_client", lambda: FakeDoctorClient(good={"vendor/a", "vendor/b"}))

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "OK" in result.output


def test_doctor_reports_context_window(monkeypatch):
    monkeypatch.setattr(cli, "load_models", lambda: ModelConfig(brain="vendor/a", hands="vendor/b"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(cli, "build_client", lambda: FakeDoctorClient(good={"vendor/a", "vendor/b"}))
    monkeypatch.setattr(cli, "resolve_context_window", lambda model, client=None: (200000, "openrouter"))

    result = runner.invoke(app, ["doctor"])
    assert "brain context window: 200000 tokens (source: openrouter)" in result.output
    assert "guessing the window" not in result.output


def test_doctor_warns_when_guessing_window(monkeypatch):
    monkeypatch.setattr(cli, "load_models", lambda: ModelConfig(brain="vendor/a", hands="vendor/b"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(cli, "build_client", lambda: FakeDoctorClient(good={"vendor/a", "vendor/b"}))
    monkeypatch.setattr(cli, "resolve_context_window", lambda model, client=None: (8192, "default"))

    result = runner.invoke(app, ["doctor"])
    assert "source: default" in result.output
    assert "RELAY_BRAIN_CONTEXT" in result.output  # the guessing note


def test_doctor_command_handles_missing_key(monkeypatch):
    # load_models is patched so it does NOT load .env; with no key, doctor must
    # exit non-zero with a clear message and never build a client.
    monkeypatch.setattr(cli, "load_models", lambda: ModelConfig(brain="b", hands="h"))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    def _no_build():
        raise AssertionError("build_client must not be called without a key")

    monkeypatch.setattr(cli, "build_client", _no_build)

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code != 0
    assert "OPENROUTER_API_KEY is not set" in result.output


# --- runs view --------------------------------------------------------------


def _record(run_id, status="completed"):
    return RunRecord(
        schema_version=1,
        run_id=run_id,
        timestamp="2026-06-02T14:41:07+00:00",
        goal="g",
        mode="planned",
        roles={"brain": "vendor/brain", "hands": "vendor/hands"},
        status=status,
        steps=2,
        escalations=0,
        parse_failures=0,
        per_role=[],
        totals={"tokens": 100, "cost_usd": 0.001, "time_s": 1.0},
        wall_time_s=2.0,
    )


def test_runs_table_limits_to_most_recent():
    records = [_record("a"), _record("b"), _record("c")]
    table = _runs_table(records, 2)
    assert table.row_count == 2


def test_runs_table_handles_limit_larger_than_log():
    table = _runs_table([_record("only")], 10)
    assert table.row_count == 1


def test_runs_command_empty_log_is_graceful(tmp_path):
    result = runner.invoke(app, ["runs", "--root", str(tmp_path)])
    assert result.exit_code == 0
    assert "no runs recorded yet" in result.output


def test_runs_command_shows_persisted_runs(tmp_path, monkeypatch):
    # Render wide so nothing wraps/truncates, letting us assert full content.
    from rich.console import Console

    monkeypatch.setattr(cli, "console", Console(width=200, legacy_windows=False))
    append_record(_record("first"), default_log_path(tmp_path))
    append_record(_record("second", status="escalation_limit"), default_log_path(tmp_path))

    result = runner.invoke(app, ["runs", "--root", str(tmp_path)])
    assert result.exit_code == 0
    assert "showing 2 of 2" in result.output
    assert "planned" in result.output
    assert "vendor/brain" in result.output  # the brain/hands model split is shown
    assert "completed" in result.output and "escalation_limit" in result.output


# --- run persistence wiring -------------------------------------------------


def _patch_planned(monkeypatch, status="completed"):
    """Patch run_planned to a network-free fake that fills the ledger + returns a result."""
    from relay.orchestrator import PlannedTaskResult
    from relay.planner import Plan, PlanStep
    from relay.telemetry import CallRecord

    monkeypatch.setattr(cli, "load_models", lambda: ModelConfig(brain="vendor/brain", hands="vendor/hands"))
    monkeypatch.setattr(cli, "_warn_if_dirty_git", lambda root: None)

    def fake_run_planned(goal, root, **kwargs):
        ledger = kwargs["ledger"]
        ledger.add(CallRecord("brain", "vendor/brain", 10, 5, 0.1, 0.001))
        ledger.add(CallRecord("hands", "vendor/hands", 20, 10, 0.2, 0.002))
        plan = Plan(steps=[PlanStep(0, "do a", status="done", outcome="did a")])
        return PlannedTaskResult(goal=goal, plan=plan, status=status, ledger=ledger)

    monkeypatch.setattr(cli, "run_planned", fake_run_planned)


def test_run_persists_record_then_no_log_skips(tmp_path, monkeypatch):
    _patch_planned(monkeypatch)
    log = default_log_path(tmp_path)

    result = runner.invoke(app, ["run", "-g", "do stuff", "--root", str(tmp_path)])
    assert result.exit_code == 0
    assert "saved run" in result.output
    records = load_records(log)
    assert len(records) == 1
    assert records[0].status == "completed"
    assert records[0].roles == {"brain": "vendor/brain", "hands": "vendor/hands"}
    assert records[0].totals["tokens"] == 45

    # --no-log must not append.
    result2 = runner.invoke(app, ["run", "-g", "again", "--root", str(tmp_path), "--no-log"])
    assert result2.exit_code == 0
    assert load_records(log) == records  # unchanged


def test_run_survives_log_write_failure(tmp_path, monkeypatch):
    _patch_planned(monkeypatch)

    def _boom(record, path):
        raise OSError("disk full")

    monkeypatch.setattr(cli, "append_record", _boom)

    result = runner.invoke(app, ["run", "-g", "x", "--root", str(tmp_path)])
    assert result.exit_code == 0  # the run itself still succeeds
    assert "could not save run log" in result.output
