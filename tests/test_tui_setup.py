"""Headless tests for the TUI setup/picker flow (Textual run_test; no network).

The setup screen's network-touching seams (model listing, slug validation) are
injected so nothing hits the network; config/auth files are isolated to a tmp dir
by the autouse conftest fixture. The load-bearing properties: masked key entry
persists 0o600, model picks write config.json, manual entry validates, and the
key field is genuinely masked (password=True).
"""

from __future__ import annotations

import asyncio
import json

import relay.secrets as secrets
import relay.store as store
from relay.config import ModelConfig
from relay.tui import RelayTuiApp, SetupScreen, setup_summary

CFG = ModelConfig(brain="vendor/brain", hands="vendor/hands")


def _app(tmp_path, **kw):
    # client=None so this isn't auto-treated as "configured"; seams avoid network.
    return RelayTuiApp(
        root=str(tmp_path), models=CFG, client=None,
        list_models_fn=lambda provider, **k: ["deepseek-v4-flash", "deepseek-v4-pro"],
        validate_fn=lambda provider, model: (True, "ok"),
        **kw,
    )


def _setup_screen(app, **over) -> SetupScreen:
    return SetupScreen(
        models=CFG,
        list_models_fn=over.get("list_models_fn", app._list_models_fn),
        validate_fn=over.get("validate_fn", app._validate_fn),
        on_saved=over.get("on_saved", app._on_setup_saved),
    )


# --- pure-ish summary (key-free) --------------------------------------------


def test_setup_summary_never_contains_a_key(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    secrets.set_key("deepseek", "sk-never-shown")
    summary = setup_summary()
    assert "key[deepseek]: present" in summary
    assert "key[openrouter]: absent" in summary
    assert "sk-never-shown" not in summary


# --- the screen opens + masked key entry ------------------------------------


def test_setup_opens_with_ctrl_s_and_key_field_is_masked(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_CONFIG_DIR", str(tmp_path / "cfg"))

    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert isinstance(app.screen, SetupScreen)
            from textual.widgets import Input

            key_input = app.screen.query_one("#key-input", Input)
            assert key_input.password is True  # keys get screenshotted -> masked
            app.screen.action_close()
            await pilot.pause()

    asyncio.run(main())


def test_masked_key_saves_to_auth_json_0600(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_CONFIG_DIR", str(tmp_path / "cfg"))

    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(_setup_screen(app))
            await pilot.pause()
            screen = app.screen
            saved = screen.save_key("deepseek", "sk-typed-in-tui")
            assert saved is True
            raw = json.loads(secrets.auth_path().read_text(encoding="utf-8"))
            assert raw["deepseek"] == {"type": "api", "key": "sk-typed-in-tui"}

    asyncio.run(main())


# --- model pick: list provider (DeepSeek) -----------------------------------


def test_list_provider_models_come_from_live_list(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_CONFIG_DIR", str(tmp_path / "cfg"))

    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(_setup_screen(app))
            await pilot.pause()
            screen = app.screen
            # DeepSeek (list) -> the injected live ids; OpenRouter (manual) -> none.
            assert screen.models_for("deepseek") == ["deepseek-v4-flash", "deepseek-v4-pro"]
            assert screen.models_for("openrouter") == []

    asyncio.run(main())


def test_pick_list_model_writes_config(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_CONFIG_DIR", str(tmp_path / "cfg"))

    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(_setup_screen(app))
            await pilot.pause()
            screen = app.screen
            ok = screen.save_role("hands", "deepseek", "deepseek-v4-flash", True)
            assert ok is True
            cfg = store.load_config()
            assert cfg["roles"]["hands"] == {
                "provider": "deepseek", "model": "deepseek-v4-flash", "thinking": True,
            }

    asyncio.run(main())


# --- manual entry validates -------------------------------------------------


def test_manual_entry_rejects_invalid_slug_and_saves_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_CONFIG_DIR", str(tmp_path / "cfg"))

    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(_setup_screen(app, validate_fn=lambda p, m: (False, "no such model")))
            await pilot.pause()
            screen = app.screen
            ok = screen.save_role("brain", "openrouter", "typo/nope", False)
            assert ok is False
            assert "rejected" in screen.status_text
            assert store.load_config() == {}  # nothing written

    asyncio.run(main())


def test_manual_entry_accepts_valid_slug(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_CONFIG_DIR", str(tmp_path / "cfg"))

    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(_setup_screen(app, validate_fn=lambda p, m: (True, "resolved")))
            await pilot.pause()
            ok = app.screen.save_role("brain", "openrouter", "openai/gpt-4o", False)
            assert ok is True
            assert store.load_config()["roles"]["brain"]["model"] == "openai/gpt-4o"

    asyncio.run(main())


# --- the live app picks up the saved config ---------------------------------


def test_save_updates_live_app_models(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)  # no project .env to shadow the config
    for var in ("RELAY_HANDS_PROVIDER", "RELAY_HANDS_MODEL", "RELAY_HANDS_THINKING"):
        monkeypatch.delenv(var, raising=False)

    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(_setup_screen(app))
            await pilot.pause()
            app.screen.save_role("hands", "deepseek", "deepseek-v4-pro", False)
            # on_saved re-resolved load_models -> the live app reflects config.json.
            assert app._models.provider_for_role("hands") == "deepseek"
            assert app._models.for_role("hands") == "deepseek-v4-pro"

    asyncio.run(main())
