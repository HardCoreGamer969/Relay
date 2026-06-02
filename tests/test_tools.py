"""Network-free tests for the executor's tools, confined to a tmp_path root."""

from __future__ import annotations

import os

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


def test_nested_path_inside_root_still_works(tmp_path):
    # The escape guard must not over-tighten: a legitimate deep path is allowed.
    tools = Tools(tmp_path)
    tools.edit("sub/dir/file.txt", "deep content")
    assert (tmp_path / "sub" / "dir" / "file.txt").read_text(encoding="utf-8") == "deep content"
    assert tools.read("sub/dir/file.txt") == "deep content"


def test_symlink_pointing_outside_root_is_rejected(tmp_path):
    # A symlink that LIVES inside the root but POINTS outside it must be refused:
    # resolve-then-check follows the link to its real (outside) target. A naive
    # raw-string check for ".." would miss this.
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"  # sibling of root -> outside the root
    outside.write_text("secret", encoding="utf-8")

    link = root / "link.txt"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("symlink creation not permitted on this platform")

    tools = Tools(root)
    with pytest.raises(PathEscapeError):
        tools.read("link.txt")
    with pytest.raises(PathEscapeError):
        tools.edit("link.txt", "overwrite attempt")

    # The outside file must be untouched by the refused edit.
    assert outside.read_text(encoding="utf-8") == "secret"
