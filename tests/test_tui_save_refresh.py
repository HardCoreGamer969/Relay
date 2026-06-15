"""Bug 1 (v0.0.19): a /model or /provider save must never leave a stale display.

Two halves, both headless (validation injected; no network):

- **Guard** -- when config.json is the resolved source (no env shadow), a save
  repaints BOTH surfaces that show models: the welcome ``#indicator`` identity line
  and the working-view ``#status`` line. ``_on_setup_saved`` re-reads ``load_models``
  and updates whichever is mounted.
- **Honest feedback** -- when an env var (``env > config``) is overriding the saved
  selection, the save has no visible effect, so the app surfaces a note naming the
  overriding ``RELAY_*`` var rather than looking stale. Precedence is unchanged --
  this only *reports* the shadow.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from textual.widgets import Input

import relay.config as config
from relay.config import ModelConfig, load_models
from relay.tui import RelayTuiApp, SegmentedControl, SelectDialog

CFG = ModelConfig(brain="vendor/old-brain", hands="vendor/old-hands")


@pytest.fixture
def no_env_shadow(monkeypatch):
    """config.json is authoritative: no .env load, no RELAY_* model/provider env."""
    monkeypatch.setattr(config, "load_env", lambda: "")
    for role in ("BRAIN", "HANDS"):
        for field in ("MODEL", "PROVIDER", "THINKING"):
            monkeypatch.delenv(f"RELAY_{role}_{field}", raising=False)


def _app(tmp_path, *, validate_fn=None, list_models_fn=None):
    return RelayTuiApp(
        root=str(tmp_path), models=CFG, client=SimpleNamespace(),
        list_models_fn=list_models_fn or (lambda provider, **k: ["deepseek-v4-flash", "deepseek-v4-pro"]),
        validate_fn=validate_fn or (lambda provider, model: (True, "ok")),
    )


# --- the guard: a save repaints the live display (no env shadow) -------------


def test_model_save_refreshes_indicator_and_status(tmp_path, no_env_shadow):
    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert "vendor/old-brain" in app._indicator_text  # the launch value
            app._cmd_model()
            await pilot.pause()
            app.screen.choose("brain")  # openrouter (manual) -> slug entry
            await pilot.pause()
            app.screen.query_one("#entry-input", Input).value = "qwen/qwen3.7-plus"
            assert app.screen.submit() is True
            await pilot.pause()
            # BOTH surfaces reflect the new model -- no stale display, no shadow note.
            assert "qwen/qwen3.7-plus" in app._indicator_text
            assert "qwen/qwen3.7-plus" in app._status_text
            assert app._save_notice == ""

    asyncio.run(main())


def test_provider_save_refreshes_status_in_working_view(tmp_path, no_env_shadow):
    """The working-view surface (``#status``) refreshes too -- /provider on hands."""
    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._view = "working"  # pretend the first goal already handed off
            app._cmd_provider()
            await pilot.pause()
            seg = app.screen
            assert isinstance(seg, SegmentedControl)
            while seg.highlighted_value() != "hands":
                seg.move(1)
            seg.select_highlighted()
            await pilot.pause()
            app.screen.choose("deepseek")  # list provider -> live list
            await pilot.pause()
            assert isinstance(app.screen, SelectDialog)
            app.screen.choose("deepseek-v4-pro")
            await pilot.pause()
            assert "deepseek-v4-pro" in app._status_text
            assert "deepseek-v4-pro" in app._indicator_text
            assert app._save_notice == ""

    asyncio.run(main())


# --- honest feedback: a shadowed save says so --------------------------------


def test_shadowed_save_warns_which_env_var_wins(tmp_path, monkeypatch):
    """With RELAY_BRAIN_MODEL set, saving a brain model via /model is overridden by
    env (env > config) -- the app names the shadowing var instead of looking stale."""
    monkeypatch.setattr(config, "load_env", lambda: "")
    monkeypatch.delenv("RELAY_HANDS_MODEL", raising=False)
    monkeypatch.delenv("RELAY_BRAIN_PROVIDER", raising=False)
    monkeypatch.delenv("RELAY_HANDS_PROVIDER", raising=False)
    monkeypatch.setenv("RELAY_BRAIN_MODEL", "env/brain-wins")

    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._cmd_model()
            await pilot.pause()
            app.screen.choose("brain")
            await pilot.pause()
            app.screen.query_one("#entry-input", Input).value = "qwen/qwen3.7-plus"
            assert app.screen.submit() is True
            await pilot.pause()
            # The save landed in config, but env wins -- the indicator shows the env
            # value (correct precedence), and the notice explains why.
            assert load_models().brain == "env/brain-wins"
            assert "env/brain-wins" in app._indicator_text
            assert "RELAY_BRAIN_MODEL" in app._save_notice
            assert "qwen/qwen3.7-plus" not in app._indicator_text

    asyncio.run(main())
