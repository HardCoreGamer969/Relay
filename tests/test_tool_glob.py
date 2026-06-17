"""Tests for the `glob` tool (v0.0.26): fast file-pattern matching.

`glob` is a read-only text-protocol tag (`<glob pattern="**/*.py"/>`, optional base
dir) parsed by `parse()` and executed via `Tools.glob`. It returns matching paths
relative to the working dir, one per line, bounded with a truncation note. No
read-guard interaction. Network-free.
"""

from __future__ import annotations

from relay.protocol import parse
from relay.tools import GLOB_MATCH_CAP, Tools


# --- parsing ----------------------------------------------------------------


def test_parse_glob_tag_pattern_only():
    result = parse('<glob pattern="**/*.py"/>')
    assert [a.kind for a in result.actions] == ["glob"]
    assert result.actions[0].pattern == "**/*.py"
    assert result.actions[0].path == "."  # base defaults to the working dir


def test_parse_glob_tag_with_base():
    result = parse('<glob pattern="*.txt" path="docs"/>')
    action = result.actions[0]
    assert action.kind == "glob" and action.pattern == "*.txt" and action.path == "docs"


# --- Tools.glob -------------------------------------------------------------


def _tree(tmp_path):
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "b.py").write_text("x", encoding="utf-8")
    (tmp_path / "pkg" / "c.txt").write_text("x", encoding="utf-8")


def test_glob_recursive_returns_relative_paths(tmp_path):
    _tree(tmp_path)
    out = Tools(tmp_path).glob("**/*.py")
    lines = out.splitlines()
    assert "a.py" in lines
    assert "pkg/b.py" in lines  # relative, posix-style, recursive
    assert "pkg/c.txt" not in lines


def test_glob_non_recursive_single_level(tmp_path):
    _tree(tmp_path)
    out = Tools(tmp_path).glob("*.py")
    assert out.splitlines() == ["a.py"]  # only top-level


def test_glob_with_base_dir(tmp_path):
    _tree(tmp_path)
    out = Tools(tmp_path).glob("*.txt", "pkg")
    assert out.splitlines() == ["pkg/c.txt"]


def test_glob_no_matches_is_clean(tmp_path):
    _tree(tmp_path)
    assert Tools(tmp_path).glob("**/*.rs") == "(no matches)"


def test_glob_bounds_large_results_with_a_note(tmp_path):
    for i in range(GLOB_MATCH_CAP + 25):
        (tmp_path / f"f{i}.log").write_text("x", encoding="utf-8")
    out = Tools(tmp_path).glob("*.log")
    lines = out.splitlines()
    assert len(lines) == GLOB_MATCH_CAP + 1  # capped rows + the truncation note
    assert lines[-1] == "... (25 more, truncated)"
