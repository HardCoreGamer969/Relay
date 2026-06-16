"""Headless tests for the /log debug-export slash command.

/log opens a two-option scope dialog (current project / full session); choosing a
scope writes a timestamped, REDACTED relay-debug-*.md to cwd and confirms the path.
The export is assembled from existing state -- no model call -- and a seeded key
value must be absent from the written file (the shareability guarantee).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from relay.config import ModelConfig
from relay.tui import RelayTuiApp, SelectDialog, visible_commands

CFG = ModelConfig(brain="vendor/brain", hands="vendor/hands")


def _app(tmp_path, **kw):
    return RelayTuiApp(root=str(tmp_path), models=CFG, client=SimpleNamespace(), **kw)


def test_log_command_is_registered(tmp_path):
    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert "log" in {c.name for c in visible_commands(app)}

    asyncio.run(main())


def test_log_opens_two_option_scope_dialog(tmp_path):
    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._cmd_log()
            await pilot.pause()
            dialog = app.screen
            assert isinstance(dialog, SelectDialog)
            assert set(dialog.visible_values()) == {"current", "session"}

    asyncio.run(main())


def test_log_writes_timestamped_md_to_cwd_and_names_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # the file lands in a tmp cwd, not the real one

    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._write_conversation("you (goal): build a flask app")
            app._cmd_log()
            await pilot.pause()
            app.screen.choose("current")  # pick the scope -> writes the file
            await pilot.pause()

            files = list(tmp_path.glob("relay-debug-*.md"))
            assert len(files) == 1
            written = files[0]
            # The confirmation names the full path.
            confirmation = "\n".join(app._conversation_lines)
            assert str(written) in confirmation
            assert "safe to attach" in confirmation.lower()
            # The file carries the expected section headers.
            body = written.read_text(encoding="utf-8")
            for header in ("# Relay debug export", "## Environment",
                           "## Conversation transcript", "## Activity log"):
                assert header in body

    asyncio.run(main())


def test_log_written_file_contains_no_seeded_secret(tmp_path, monkeypatch):
    """The shareability guarantee at the command level: a key set in the live config
    is stripped from the written file (present as 'present', never as its value)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    secret = "sk-LIVEKEY-deadbeefdeadbeefdeadbeef0123456789"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)

    async def main():
        # client=None so the live key is what _has_usable_config / _live_key_values see.
        app = RelayTuiApp(root=str(tmp_path), models=CFG, client=None)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.pause()
            # Even if a transcript line somehow held the key, it must not survive.
            app._write_conversation(f"you (goal): use {secret} please")
            app._write_debug_log("session")
            await pilot.pause()

            files = list(tmp_path.glob("relay-debug-*.md"))
            assert len(files) == 1
            body = files[0].read_text(encoding="utf-8")
            assert secret not in body                 # the value is gone...
            assert "openrouter: present" in body      # ...presence is still reported

    asyncio.run(main())


def test_log_scopes_render_differently(tmp_path):
    """Current uses the current run's structured transcript (empty before any run);
    full session uses the session-spanning conversation buffer -- so a prior goal
    line appears only in the session export."""

    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            # A line in the session buffer with no backing structured transcript turn.
            app._write_conversation("you (goal): an earlier project")
            current = app._build_debug_bundle("current", timestamp="t1")
            session = app._build_debug_bundle("session", timestamp="t2")
            assert "Current project" in current and "Full session" in session
            # The buffer line is in session only; current renders the (empty) structured
            # transcript of the current run.
            assert "an earlier project" in session
            assert "an earlier project" not in current

    asyncio.run(main())


def test_log_write_failure_is_friendly_not_a_traceback(tmp_path, monkeypatch):
    """A write failure surfaces a friendly line, never a traceback."""
    monkeypatch.chdir(tmp_path)

    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()

            def _boom(*a, **k):
                raise OSError(13, "Permission denied")

            monkeypatch.setattr("pathlib.Path.write_text", _boom)
            app._write_debug_log("current")
            await pilot.pause()
            convo = "\n".join(app._conversation_lines)
            assert "could not write the debug log" in convo
            assert "Permission denied" in convo

    asyncio.run(main())


def test_log_makes_zero_model_calls(tmp_path, monkeypatch):
    """Building/writing the bundle never calls the model client."""
    monkeypatch.chdir(tmp_path)

    class _CountingClient:
        def __init__(self):
            self.calls = 0

        def __getattr__(self, _name):
            self.calls += 1
            raise AssertionError("the /log export must not call the model client")

    client = _CountingClient()

    async def main():
        app = RelayTuiApp(root=str(tmp_path), models=CFG, client=client)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._write_debug_log("session")
            await pilot.pause()
            assert client.calls == 0
            assert list(tmp_path.glob("relay-debug-*.md"))

    asyncio.run(main())
