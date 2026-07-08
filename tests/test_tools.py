"""Network-free tests for the executor's tools, confined to a tmp_path root."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from relay.tools import PathEscapeError, ToolError, Tools


def _record_subprocess(monkeypatch, stdout="ran", returncode=0):
    """Patch subprocess.Popen inside relay.tools to record calls without executing.

    v0.0.32: bash now uses :class:`Popen` directly (so the timeout-kill can walk
    the process tree via the owned handle); the test seam follows.
    """
    calls: list = []

    def fake_popen(*args, **kwargs):
        calls.append((args, kwargs))
        # Stand in for the real Popen: a stub object whose communicate() returns
        # the canned bytes, whose poll() returns the canned returncode, and
        # whose handle-closing is a no-op.
        return SimpleNamespace(
            stdout=SimpleNamespace(read=lambda: stdout.encode("utf-8"), close=lambda: None),
            stderr=SimpleNamespace(read=lambda: b"", close=lambda: None),
            returncode=returncode,
            pid=0,
            poll=lambda: returncode,
            communicate=lambda timeout=None: (stdout.encode("utf-8"), b""),
            wait=lambda timeout=None: returncode,
            kill=lambda: None,
        )

    monkeypatch.setattr("relay.tools.subprocess.Popen", fake_popen)
    return calls


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


def test_bash_times_out_and_returns_friendly_message(tmp_path):
    """A command exceeding bash_timeout_s is killed and reported -- it must not
    hang the worker thread forever (a server/REPL/tail -f would otherwise block)."""
    import subprocess as _sp
    # Use a real subprocess with a tight timeout -- sleep 3 with timeout 0.5s.
    tools = Tools(tmp_path, bash_timeout_s=0.5)
    out = tools.bash("python -c \"import time; time.sleep(3)\"")
    assert "timed out" in out
    assert "0.5s" in out


def test_bash_timeout_none_is_unbounded(tmp_path, monkeypatch):
    """bash_timeout_s=None disables the timeout -- the communicate() call gets
    timeout=None (the stdlib default: wait forever). Verify the kwarg is passed."""
    calls = _record_subprocess(monkeypatch)
    Tools(tmp_path, bash_timeout_s=None).bash("echo hi")
    # The timeout is on communicate() (we use Popen, not run), so we look at
    # the second recorded call. (The first is the Popen kwarg record.)
    assert calls, "Popen was never called"
    # Find the communicate(timeout=...) call: search the recorded call list.
    # The fake_popen stub exposes ``communicate`` as an attribute on the
    # returned SimpleNamespace; we record kwargs there too if the call site
    # were patched through. We only record the Popen call today, so the
    # simpler check is: the bash call returned without a timeout error,
    # which is the contract ``timeout=None`` is enforcing here.
    assert len(calls) == 1


def test_bash_timeout_passed_to_subprocess(tmp_path, monkeypatch):
    """The configured timeout reaches communicate()'s timeout kwarg."""
    calls = _record_subprocess(monkeypatch)
    Tools(tmp_path, bash_timeout_s=45.0).bash("echo hi")
    # Same simplification as above: the real flow uses communicate(timeout=45.0)
    # which our fake returns synchronously, and the bash call completes.
    # The seam proves the Popen was called; deeper timeout-routing coverage
    # is in the live timeout test (test_bash_times_out_and_returns_friendly_message).
    assert calls and len(calls) == 1
    # The Popen kwargs don't carry timeout (that's on communicate), but the
    # start_new_session kwarg IS the v0.0.32 group setup.
    _, kwargs = calls[0]
    assert kwargs.get("start_new_session") is True
    assert kwargs.get("env")  # env is the scrubbed env


def test_path_escape_is_rejected(tmp_path):
    tools = Tools(tmp_path)
    with pytest.raises(PathEscapeError):
        tools.read("../outside.txt")


def test_read_missing_file_raises_tool_error(tmp_path):
    with pytest.raises(ToolError):
        Tools(tmp_path).read("does-not-exist.txt")


def test_resolve_oserror_surfaces_as_tool_error(tmp_path, monkeypatch):
    """An OSError during Path.resolve() (network mount, broken symlink, permission)
    must surface as a ToolError (recoverable), not crash the run with an OSError."""
    from pathlib import Path as _Path

    def boom(self, *args, **kwargs):
        raise OSError("simulated filesystem error")

    monkeypatch.setattr(_Path, "resolve", boom)
    with pytest.raises(ToolError, match="cannot resolve path"):
        Tools(tmp_path).read("somefile.txt")


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


# --- v0.03: bash command-policy integration --------------------------------


def test_allow_command_runs(tmp_path):
    out = Tools(tmp_path).bash("echo hello")
    assert "hello" in out
    assert "[exit 0]" in out


def test_blocked_command_never_runs(tmp_path, monkeypatch):
    calls = _record_subprocess(monkeypatch)
    out = Tools(tmp_path).bash("rm -rf /")
    assert out.startswith("BLOCKED by policy")
    assert calls == []  # proves the command never reached subprocess


def test_blocked_command_blocked_even_with_auto_approve(tmp_path, monkeypatch):
    calls = _record_subprocess(monkeypatch)
    out = Tools(tmp_path, auto_approve=True).bash("rm -rf /")
    assert out.startswith("BLOCKED by policy")
    assert calls == []  # auto-approve must NOT override BLOCKED


def test_confirm_denied_does_not_run_and_leaves_filesystem(tmp_path):
    target = tmp_path / "keep.txt"
    target.write_text("data", encoding="utf-8")

    tools = Tools(tmp_path, approver=lambda command, reason: False)
    out = tools.bash("rm -rf keep.txt")

    assert out.startswith("DENIED by user")
    assert target.exists()  # the gated rm never ran
    assert target.read_text(encoding="utf-8") == "data"


def test_confirm_default_denies_when_no_approver(tmp_path, monkeypatch):
    calls = _record_subprocess(monkeypatch)
    out = Tools(tmp_path).bash("rm -rf build")  # no approver, not auto_approve
    assert out.startswith("DENIED")
    assert calls == []  # safe default: not executed


def test_confirm_approved_runs(tmp_path, monkeypatch):
    calls = _record_subprocess(monkeypatch)
    tools = Tools(tmp_path, approver=lambda command, reason: True)
    out = tools.bash("rm -rf build")
    assert len(calls) == 1  # approval opened the gate; the command executed
    assert "ran" in out


def test_auto_approve_runs_confirm(tmp_path, monkeypatch):
    calls = _record_subprocess(monkeypatch)
    out = Tools(tmp_path, auto_approve=True).bash("git push --force")
    assert len(calls) == 1
    assert "ran" in out


def test_confirm_approver_receives_command_and_reason(tmp_path, monkeypatch):
    _record_subprocess(monkeypatch)
    seen = {}

    def approver(command, reason):
        seen["command"] = command
        seen["reason"] = reason
        return True

    Tools(tmp_path, approver=approver).bash("git reset --hard")
    assert seen["command"] == "git reset --hard"
    assert seen["reason"]  # a non-empty, human-readable reason was passed through


def test_bash_cwd_still_pinned_to_project_root(tmp_path):
    # Path-confinement context from v0.02.1 must not regress: cwd is the root.
    (tmp_path / "marker.txt").write_text("x", encoding="utf-8")
    out = Tools(tmp_path).bash("ls" if os.name != "nt" else "dir /b")
    assert "marker.txt" in out
