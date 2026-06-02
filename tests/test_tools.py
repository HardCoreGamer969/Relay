"""Network-free tests for the executor's tools, confined to a tmp_path root."""

from __future__ import annotations

import pytest

from relay.tools import PathEscapeError, ToolError, Tools


def test_edit_then_read_roundtrip(tmp_path):
    tools = Tools(tmp_path)
    confirmation = tools.edit("notes/hello.txt", "hi from relay")

    assert "hello.txt" in confirmation
    written = (tmp_path / "notes" / "hello.txt").read_text(encoding="utf-8")
    assert written == "hi from relay"
    assert tools.read("notes/hello.txt") == "hi from relay"


def test_edit_creates_parent_directories(tmp_path):
    tools = Tools(tmp_path)
    tools.edit("a/b/c/deep.txt", "x")
    assert (tmp_path / "a" / "b" / "c" / "deep.txt").exists()


def test_list_marks_directories(tmp_path):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    out = Tools(tmp_path).list(".")
    assert "a.txt" in out
    assert "sub/" in out


def test_grep_returns_matching_lines_with_numbers(tmp_path):
    (tmp_path / "f.txt").write_text("alpha\nbeta TODO here\ngamma\n", encoding="utf-8")
    out = Tools(tmp_path).grep("TODO", "f.txt")
    assert "2: beta TODO here" in out


def test_grep_reports_no_matches(tmp_path):
    (tmp_path / "f.txt").write_text("nothing here\n", encoding="utf-8")
    assert Tools(tmp_path).grep("ABSENT", "f.txt") == "(no matches)"


def test_bash_captures_stdout_and_exit_code(tmp_path):
    out = Tools(tmp_path).bash("echo hello")
    assert "hello" in out
    assert "[exit 0]" in out


def test_path_escape_is_rejected(tmp_path):
    tools = Tools(tmp_path)
    with pytest.raises(PathEscapeError):
        tools.read("../outside.txt")


def test_read_missing_file_raises_tool_error(tmp_path):
    with pytest.raises(ToolError):
        Tools(tmp_path).read("does-not-exist.txt")
