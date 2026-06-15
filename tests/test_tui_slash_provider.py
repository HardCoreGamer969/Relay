"""Headless tests for /provider: role toggle -> provider -> model.

Network-touching work (list_models / validate_model) is injected; nothing hits
the network. Load-bearing rules: per-role isolation (brain-only never touches
hands and vice versa), ``both`` runs the model step twice (brain then hands), and
the model step reuses the SAME validate/list/persist path as /model (no fork).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import relay.tui as tui
import relay.store as store
from relay.config import ModelConfig
from relay.tui import RelayTuiApp, SegmentedControl, SelectDialog

# Both roles start on openrouter; /provider will move them onto deepseek (a `list`
# provider) so the model step shows the injected live list.
CFG = ModelConfig(brain="anthropic/old-brain", hands="anthropic/old-hands")


def _app(tmp_path, *, validate_fn=None, list_models_fn=None):
    return RelayTuiApp(
        root=str(tmp_path), models=CFG, client=SimpleNamespace(),
        list_models_fn=list_models_fn or (lambda provider, **k: ["deepseek-v4-flash", "deepseek-v4-pro"]),
        validate_fn=validate_fn or (lambda provider, model: (True, "ok")),
    )


async def _choose_scope(pilot, app, scope):
    """Drive the /provider role toggle to ``scope`` and commit."""
    app._cmd_provider()
    await pilot.pause()
    seg = app.screen
    assert isinstance(seg, SegmentedControl)
    while seg.highlighted_value() != scope:
        seg.move(1)
    seg.select_highlighted()
    await pilot.pause()


async def _choose_provider(pilot, app, provider):
    dialog = app.screen
    assert isinstance(dialog, SelectDialog)
    dialog.choose(provider)
    await pilot.pause()


def _seed_config():
    """A known starting config so isolation can be checked byte-for-byte: the flow
    must leave the role it didn't touch EXACTLY as seeded (persist_role builds on
    the existing config, so the other role is never rewritten by the flow)."""
    store.save_config({
        "version": 1,
        "providers": {"openrouter": {"enabled": True}, "deepseek": {"enabled": True}},
        "roles": {
            "brain": {"provider": "openrouter", "model": "brain-orig", "thinking": False},
            "hands": {"provider": "openrouter", "model": "hands-orig", "thinking": False},
        },
    })


# --- per-role isolation -----------------------------------------------------


def test_provider_brain_only_never_touches_hands(tmp_path):
    async def main():
        _seed_config()
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _choose_scope(pilot, app, "brain")
            await _choose_provider(pilot, app, "deepseek")
            # model step for brain (deepseek is a list provider -> live list)
            assert isinstance(app.screen, SelectDialog)
            app.screen.choose("deepseek-v4-pro")
            await pilot.pause()

            cfg = store.load_config()
            assert cfg["roles"]["brain"] == {
                "provider": "deepseek", "model": "deepseek-v4-pro", "thinking": False,
            }
            # hands is EXACTLY as seeded -- the flow never touched it.
            assert cfg["roles"]["hands"] == {
                "provider": "openrouter", "model": "hands-orig", "thinking": False,
            }

    asyncio.run(main())


def test_provider_hands_only_never_touches_brain(tmp_path):
    async def main():
        _seed_config()
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _choose_scope(pilot, app, "hands")
            await _choose_provider(pilot, app, "deepseek")
            assert isinstance(app.screen, SelectDialog)
            app.screen.choose("deepseek-v4-flash")
            await pilot.pause()

            cfg = store.load_config()
            assert cfg["roles"]["hands"] == {
                "provider": "deepseek", "model": "deepseek-v4-flash", "thinking": False,
            }
            # brain is EXACTLY as seeded -- the flow never touched it.
            assert cfg["roles"]["brain"] == {
                "provider": "openrouter", "model": "brain-orig", "thinking": False,
            }

    asyncio.run(main())


# --- both: model step runs twice, independently -----------------------------


def test_provider_both_runs_model_step_twice(tmp_path):
    picks = []

    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _choose_scope(pilot, app, "both")
            await _choose_provider(pilot, app, "deepseek")

            # First model dialog: brain.
            assert isinstance(app.screen, SelectDialog)
            picks.append(app.screen)
            app.screen.choose("deepseek-v4-pro")        # brain -> pro
            await pilot.pause()
            await pilot.pause()  # let call_after_refresh chain into the hands step

            # Second model dialog: hands (a distinct dialog instance, independent pick).
            assert isinstance(app.screen, SelectDialog)
            assert app.screen is not picks[0]
            app.screen.choose("deepseek-v4-flash")      # hands -> flash
            await pilot.pause()

            cfg = store.load_config()
            assert cfg["roles"]["brain"] == {
                "provider": "deepseek", "model": "deepseek-v4-pro", "thinking": False,
            }
            assert cfg["roles"]["hands"] == {
                "provider": "deepseek", "model": "deepseek-v4-flash", "thinking": False,
            }

    asyncio.run(main())


# --- reuse, not fork --------------------------------------------------------


def test_provider_reuses_validate_and_persist(tmp_path, monkeypatch):
    """/provider's model step goes through the SAME validate_model + persist_role
    path as /model -- proven by spying on the shared seam + persist_role."""
    validated = []
    persisted = []

    def spy_validate(provider, model):
        validated.append((provider, model))
        return True, "resolved"

    real_persist = tui.persist_role

    def spy_persist(role, provider, model, thinking, *, validate_fn=None):
        persisted.append((role, provider, model))
        return real_persist(role, provider, model, thinking, validate_fn=validate_fn)

    monkeypatch.setattr(tui, "persist_role", spy_persist)

    async def main():
        # OpenRouter is a MANUAL provider -> the slug TextEntryDialog runs validate.
        app = _app(tmp_path, validate_fn=spy_validate)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _choose_scope(pilot, app, "brain")
            await _choose_provider(pilot, app, "openrouter")
            entry = app.screen  # manual -> TextEntryDialog
            from textual.widgets import Input

            entry.query_one("#entry-input", Input).value = "openai/gpt-4o"
            assert entry.submit() is True
            await pilot.pause()

            assert ("openrouter", "openai/gpt-4o") in validated      # shared validate
            assert ("brain", "openrouter", "openai/gpt-4o") in persisted  # shared persist

    asyncio.run(main())


# --- cross-provider validation (Bug 3 guard, v0.0.19) -----------------------


def test_provider_validates_against_the_newly_picked_provider(tmp_path):
    """Switching a role's provider must validate the model against the NEWLY-picked
    provider, never the role's pre-existing one. A slug validated against the wrong
    endpoint is exactly what 400s in live use -- so spy validate_model and assert it
    only ever saw the new provider. (openrouter -> deepseek; list provider.)"""
    validated = []

    def spy_validate(provider, model):
        validated.append((provider, model))
        return True, "ok"

    async def main():
        _seed_config()  # hands starts on openrouter
        app = _app(tmp_path, validate_fn=spy_validate)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _choose_scope(pilot, app, "hands")
            await _choose_provider(pilot, app, "deepseek")  # NEW != current (openrouter)
            assert isinstance(app.screen, SelectDialog)
            app.screen.choose("deepseek-v4-pro")
            await pilot.pause()

            assert ("deepseek", "deepseek-v4-pro") in validated
            assert all(p == "deepseek" for p, _ in validated)  # never the old provider
            assert store.load_config()["roles"]["hands"] == {
                "provider": "deepseek", "model": "deepseek-v4-pro", "thinking": False,
            }

    asyncio.run(main())


def test_provider_switch_to_manual_validates_against_new_provider(tmp_path):
    """The other direction: a role CURRENTLY on deepseek, switched to openrouter
    (manual), validates the typed slug against openrouter -- not the stale deepseek."""
    from textual.widgets import Input

    validated = []

    def spy_validate(provider, model):
        validated.append((provider, model))
        return True, "ok"

    async def main():
        store.save_config({
            "version": 1,
            "roles": {
                "brain": {"provider": "openrouter", "model": "brain-orig", "thinking": False},
                "hands": {"provider": "deepseek", "model": "deepseek-v4-flash", "thinking": False},
            },
        })
        app = RelayTuiApp(
            root=str(tmp_path),
            models=ModelConfig(brain="brain-orig", hands="deepseek-v4-flash", hands_provider="deepseek"),
            client=SimpleNamespace(),
            list_models_fn=lambda provider, **k: ["deepseek-v4-flash"],
            validate_fn=spy_validate,
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            await _choose_scope(pilot, app, "hands")
            await _choose_provider(pilot, app, "openrouter")  # NEW != current (deepseek)
            entry = app.screen  # manual -> TextEntryDialog
            entry.query_one("#entry-input", Input).value = "openai/gpt-4o"
            assert entry.submit() is True
            await pilot.pause()

            assert ("openrouter", "openai/gpt-4o") in validated
            assert all(p == "openrouter" for p, _ in validated)  # never the stale deepseek
            assert store.load_config()["roles"]["hands"] == {
                "provider": "openrouter", "model": "openai/gpt-4o", "thinking": False,
            }

    asyncio.run(main())


# --- no inline args ---------------------------------------------------------


def test_provider_takes_no_inline_args(tmp_path):
    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            # Running /provider opens the role toggle -- it reads nothing from the
            # prompt text (role/provider/model are all dialog choices).
            app._cmd_provider()
            await pilot.pause()
            assert isinstance(app.screen, SegmentedControl)
            app.screen.action_close()
            await pilot.pause()

    asyncio.run(main())
