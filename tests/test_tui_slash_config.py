"""Headless tests for the config slash commands: /help /model /key /config.

These reuse the v0.0.16 functions (validate_model / list_models / secrets.set_key /
persist config) -- the tests inject the listing/validation seams so nothing hits
the network. Load-bearing rules: a key is entered ONLY via the masked dialog
(never the prompt text), and /config never reveals a key.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from textual.widgets import Input

import relay.secrets as secrets
import relay.store as store
import relay.tui as tui
from relay.config import ModelConfig
from relay.tui import RelayTuiApp, SelectDialog, TextEntryDialog


def _app(tmp_path, *, models=None, validate_fn=None, list_models_fn=None):
    return RelayTuiApp(
        root=str(tmp_path),
        models=models or ModelConfig(brain="vendor/brain", hands="vendor/hands"),
        client=SimpleNamespace(),
        list_models_fn=list_models_fn or (lambda provider, **k: ["deepseek-v4-flash", "deepseek-v4-pro"]),
        validate_fn=validate_fn or (lambda provider, model: (True, "ok")),
    )


# --- /help ------------------------------------------------------------------


def test_help_lists_all_commands(tmp_path):
    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._cmd_help()
            await pilot.pause()
            dialog = app.screen
            assert isinstance(dialog, SelectDialog)
            assert set(dialog.visible_values()) == {c.name for c in tui.COMMANDS}

    asyncio.run(main())


# --- /model (list provider: DeepSeek) ---------------------------------------


def test_model_list_provider_walks_role_then_model_and_persists(tmp_path):
    cfg = ModelConfig(brain="anthropic/x", hands="deepseek-v4-flash", hands_provider="deepseek")

    async def main():
        app = _app(tmp_path, models=cfg)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._cmd_model()                 # role picker
            await pilot.pause()
            app.screen.choose("hands")       # -> model picker for hands (deepseek/list)
            await pilot.pause()
            assert isinstance(app.screen, SelectDialog)
            # the model options are the injected LIVE list (no network)
            assert app.screen.visible_values() == ["deepseek-v4-flash", "deepseek-v4-pro"]
            app.screen.choose("deepseek-v4-pro")
            await pilot.pause()
            saved = store.load_config()["roles"]["hands"]
            assert saved == {"provider": "deepseek", "model": "deepseek-v4-pro", "thinking": False}

    asyncio.run(main())


# --- /model (manual provider: OpenRouter) -----------------------------------


def test_model_manual_provider_validates_then_persists(tmp_path):
    seen = {}

    def spy_validate(provider, model):
        seen["called"] = (provider, model)
        return True, "resolved"

    cfg = ModelConfig(brain="anthropic/old", hands="vendor/hands")  # brain = openrouter (manual)

    async def main():
        app = _app(tmp_path, models=cfg, validate_fn=spy_validate)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._cmd_model()
            await pilot.pause()
            app.screen.choose("brain")       # -> manual slug entry (TextEntryDialog)
            await pilot.pause()
            entry = app.screen
            assert isinstance(entry, TextEntryDialog)
            entry.query_one("#entry-input", Input).value = "openai/gpt-4o"
            assert entry.submit() is True
            # validation went through the SHARED (injected) validate path -- not a fork.
            assert seen["called"] == ("openrouter", "openai/gpt-4o")
            assert store.load_config()["roles"]["brain"]["model"] == "openai/gpt-4o"

    asyncio.run(main())


def test_model_manual_invalid_slug_saves_nothing(tmp_path):
    cfg = ModelConfig(brain="anthropic/old", hands="vendor/hands")

    async def main():
        app = _app(tmp_path, models=cfg, validate_fn=lambda p, m: (False, "no such model"))
        async with app.run_test() as pilot:
            await pilot.pause()
            app._cmd_model()
            await pilot.pause()
            app.screen.choose("brain")
            await pilot.pause()
            entry = app.screen
            entry.query_one("#entry-input", Input).value = "typo/nope"
            assert entry.submit() is False          # rejected inline
            assert "no such model" in entry.status_text
            assert store.load_config() == {}        # nothing written

    asyncio.run(main())


# --- /key (masked; reuses secrets.set_key; never from the prompt) -----------


def test_key_uses_masked_dialog_and_saves_0600(tmp_path):
    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._cmd_key()                   # provider picker
            await pilot.pause()
            app.screen.choose("deepseek")    # -> masked key entry
            await pilot.pause()
            entry = app.screen
            assert isinstance(entry, TextEntryDialog)
            assert entry.query_one("#entry-input", Input).password is True   # masked
            entry.query_one("#entry-input", Input).value = "sk-typed-in-dialog"
            assert entry.submit() is True
            raw = json.loads(secrets.auth_path().read_text(encoding="utf-8"))
            assert raw["deepseek"] == {"type": "api", "key": "sk-typed-in-dialog"}

    asyncio.run(main())


def test_no_inline_secret_key_never_read_from_prompt(tmp_path, monkeypatch):
    """The load-bearing guard: a key is entered ONLY via the masked dialog field --
    there is NO path that reads a key out of the chat prompt text."""
    captured = {}
    real_set_key = secrets.set_key

    def watched_set_key(provider, key, **kw):
        captured["provider_key"] = (provider, key)
        return real_set_key(provider, key, **kw)

    monkeypatch.setattr(tui, "secrets_set_key", watched_set_key)

    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            # Type "/key" in the PROMPT and run it via the popover (as a user would).
            app.query_one("#prompt", Input).value = "/key"
            await pilot.pause()
            await pilot.press("enter")       # runs /key -> opens provider picker
            await pilot.pause()
            # The prompt was cleared; "/key" was the trigger, not a value.
            assert app.query_one("#prompt", Input).value == ""
            app.screen.choose("openrouter")  # -> masked entry
            await pilot.pause()
            entry = app.screen
            # The key value is set on the DIALOG's field, never the prompt.
            entry.query_one("#entry-input", Input).value = "sk-only-in-dialog"
            entry.submit()
            # set_key received the dialog value, and the prompt never held a key.
            assert captured["provider_key"] == ("openrouter", "sk-only-in-dialog")
            assert "sk-only-in-dialog" not in app.query_one("#prompt", Input).value

    asyncio.run(main())


# --- /config (resolution; never the key) ------------------------------------


def test_config_shows_resolution_without_the_key(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    secrets.set_key("deepseek", "sk-super-secret")

    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._cmd_config()
            await pilot.pause()
            dialog = app.screen
            assert isinstance(dialog, SelectDialog)
            # Serialize only the displayed fields (options also carry callables).
            blob = json.dumps([
                {k: o.get(k) for k in ("title", "value", "description", "category")}
                for o in dialog._options
            ])
            assert "sk-super-secret" not in blob          # never the key value
            assert "key[deepseek]: present" in blob       # presence only
            assert "key[openrouter]: absent" in blob

    asyncio.run(main())
