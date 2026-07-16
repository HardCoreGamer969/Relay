"""Hermetic tests for protocol fitness lab (C2 / ``relay probe``)."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from relay.cli import app
from relay.probe import (
    EXIT_FIT,
    EXIT_UNFIT,
    band_for,
    grade_transcript,
    probe_fixture,
    probe_offline,
)

FIXTURES = Path(__file__).parent / "fixtures" / "protocol_lab"
runner = CliRunner()


def test_band_thresholds():
    assert band_for(70) == "fit"
    assert band_for(69) == "weak"
    assert band_for(40) == "weak"
    assert band_for(39) == "unfit"


def test_grade_fit_fixture():
    result = probe_fixture(FIXTURES / "fit_brain_hands.json", role="both")
    assert result.overall >= 70
    assert result.band == "fit"
    assert result.exit_code() == EXIT_FIT
    names = {d.name for d in result.dimensions}
    assert "plan_shape" in names
    assert "tag_discipline" in names
    assert "action_validity" in names


def test_grade_unfit_fixture():
    result = probe_fixture(FIXTURES / "unfit_prose.json", role="both")
    assert result.overall < 40
    assert result.band == "unfit"
    assert result.exit_code() == EXIT_UNFIT


def test_grade_brain_only():
    result = probe_fixture(FIXTURES / "fit_brain_hands.json", role="brain")
    assert all(
        d.name in ("plan_shape", "tag_discipline", "terminator_clarity")
        for d in result.dimensions
    )
    assert result.overall >= 70


def test_grade_hands_only_unfit():
    result = probe_fixture(FIXTURES / "unfit_prose.json", role="hands")
    assert result.band in ("weak", "unfit")
    assert result.overall < 70


def test_probe_offline_matches_slug():
    result = probe_offline(
        "vendor/fit-model",
        role="both",
        fixtures_dir=FIXTURES,
    )
    assert result.fixture == "fit_brain_hands.json" or "fit" in result.fixture
    assert result.band == "fit"


def test_cli_probe_fit_exit_zero():
    result = runner.invoke(
        app,
        [
            "probe",
            "vendor/fit-model",
            "--role",
            "both",
            "--fixture",
            str(FIXTURES / "fit_brain_hands.json"),
        ],
    )
    assert result.exit_code == EXIT_FIT
    assert "plan_shape" in result.stdout


def test_cli_probe_unfit_exit_three():
    result = runner.invoke(
        app,
        [
            "probe",
            "vendor/unfit-model",
            "--fixture",
            str(FIXTURES / "unfit_prose.json"),
        ],
    )
    assert result.exit_code == EXIT_UNFIT


def test_grade_transcript_dict_direct():
    data = {
        "slug": "x",
        "brain": [{"text": "<abort>impossible</abort>"}],
        "hands": [],
    }
    result = grade_transcript(data, role="brain")
    assert result.overall >= 40  # abort is valid brain terminal
