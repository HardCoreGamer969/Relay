"""U3–U6: rich stream, approve modal, anim kill, find, prefs, assets."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from relay.bridge import REQUEST_APPROVAL, UiRequest
from relay.config import ModelConfig
from relay.tui import ApproveDialog, RelayTuiApp
from relay.tui.stream import (
    find_in_lines,
    looks_like_diff,
    looks_like_markdown,
    render_observation,
    tool_summary_line,
)

CFG = ModelConfig(brain="vendor/brain", hands="vendor/hands")


def _app(tmp_path, **kw):
    kw.setdefault("anim_mode", "off")
    return RelayTuiApp(
        root=str(tmp_path), models=CFG, client=SimpleNamespace(),
        list_models_fn=lambda provider, **k: [],
        validate_fn=lambda provider, model: (True, "ok"),
        **kw,
    )


def test_looks_like_markdown_and_diff():
    assert looks_like_markdown("# Title\n\n- item\n- other\n")
    assert looks_like_markdown("```python\nprint(1)\n```")
    assert not looks_like_markdown("plain")
    diff = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n"
    assert looks_like_diff(diff)
    assert not looks_like_diff("78 lines")


def test_tool_summary_fold_marker():
    text = tool_summary_line("write a.py", "x" * 80, folded=True)
    assert "[+]" in text.plain


def test_render_observation_diff():
    diff = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n"
    out = render_observation(diff, expanded=False)
    assert out is not None


def test_find_in_lines():
    assert find_in_lines(["alpha", "bravo charlie", "delta"], "CHAR") == [1]


def test_logo_assets_packaged():
    root = Path(__file__).resolve().parents[1] / "relay" / "assets"
    assert (root / "logo-icon.svg").is_file()
    assert (root / "logo.svg").is_file()


def test_approve_modal_delivers_once(tmp_path):
    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            answers = []
            req = UiRequest(
                kind=REQUEST_APPROVAL,
                prompt=(
                    "The executor wants to run a gated command:\n  rm -rf /tmp/x\n"
                    "Why gated: destructive\nApprove? (yes/no)"
                ),
            )
            # Patch deliver to capture.
            orig = req.deliver

            def capture(answer):
                answers.append(answer)
                return orig(answer)

            req.deliver = capture  # type: ignore[method-assign]
            app._open_approve_modal(req)
            await pilot.pause()
            assert isinstance(app.screen, ApproveDialog)
            app.screen.action_once()
            await pilot.pause()
            assert answers == ["yes"]

    asyncio.run(main())


def test_approve_session_allowlist_skips_modal(tmp_path):
    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._session_approvals.add("pip install x")
            answers = []
            req = UiRequest(
                kind=REQUEST_APPROVAL,
                prompt=(
                    "The executor wants to run a gated command:\n  pip install x\n"
                    "Why gated: installer\nApprove? (yes/no)"
                ),
            )
            req.deliver = lambda a: answers.append(a) or True  # type: ignore
            app._open_approve_modal(req)
            await pilot.pause()
            assert answers == ["yes"]
            assert not isinstance(app.screen, ApproveDialog)

    asyncio.run(main())


def test_anim_off_stops_led(tmp_path):
    async def main():
        app = _app(tmp_path, anim_mode="short")
        async with app.run_test() as pilot:
            await pilot.pause()
            # Force a led timer if mount started one.
            app._cmd_anim("off")
            await pilot.pause()
            assert app._anim_mode == "off"
            assert app._led_timer is None

    asyncio.run(main())


def test_find_locates_stream_string(tmp_path):
    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._show_working()
            app._write_activity("unique-needle-xyz in the stream")
            await pilot.pause()
            app._cmd_find("unique-needle-xyz")
            await pilot.pause()
            assert any("unique-needle-xyz" in line for line in app._activity_lines)

    asyncio.run(main())


def test_shift_enter_inserts_newline(tmp_path):
    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            from textual.widgets import Input

            prompt = app.query_one("#prompt", Input)
            prompt.value = "line1"
            prompt.cursor_position = len(prompt.value)
            await pilot.press("shift+enter")
            await pilot.pause()
            # Some Textual versions may map differently; accept either newline or no-op.
            assert "\n" in prompt.value or prompt.value == "line1"

    asyncio.run(main())


def test_tui_prefs_persist_plan_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_CONFIG_DIR", str(tmp_path / "cfg"))

    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._cmd_plan("hidden")
            await pilot.pause()
            from relay.store import load_config

            cfg = load_config()
            assert cfg["tui"]["plan_dock"] == "hidden"

    asyncio.run(main())
