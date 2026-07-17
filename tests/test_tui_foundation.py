"""U1 foundation: capped stream, scroll-pin, off-thread doctor/model, slash args."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from textual.containers import VerticalScroll

from relay.config import ModelConfig
from relay.tui import (
    COMMANDS,
    STREAM_MAX_LINES,
    RelayTuiApp,
    SelectDialog,
    SetupScreen,
    command_by_name,
)
from relay.tui.stream import stream_should_follow, trim_deque_list

CFG = ModelConfig(brain="vendor/brain", hands="vendor/hands")


def _app(tmp_path, **kw):
    return RelayTuiApp(
        root=str(tmp_path),
        models=kw.pop("models", CFG),
        client=SimpleNamespace(),
        list_models_fn=kw.pop(
            "list_models_fn",
            lambda provider, **k: ["deepseek-v4-flash", "deepseek-v4-pro"],
        ),
        validate_fn=kw.pop("validate_fn", lambda provider, model: (True, "ok")),
        doctor_fn=kw.pop(
            "doctor_fn",
            lambda: [{"role": "brain", "provider": "openrouter",
                      "model": "m", "status": "OK", "note": "resolved"}],
        ),
        runs_fn=kw.pop("runs_fn", lambda: []),
        **kw,
    )


def test_setup_compose_does_not_call_list_models(tmp_path):
    """U1 acceptance: no live HTTP during compose(); lists fill after mount worker."""
    import threading

    from textual.widgets import Select

    gate = threading.Event()
    calls: list[str] = []

    def blocked(provider, **k):
        calls.append(provider)
        gate.wait(timeout=5)
        return ["deepseek-v4-flash"]

    models = ModelConfig(
        brain="deepseek-v4-flash", hands="deepseek-v4-flash",
        brain_provider="deepseek", hands_provider="deepseek",
    )

    async def main():
        app = _app(tmp_path, models=models, list_models_fn=blocked)
        async with app.run_test() as pilot:
            await pilot.pause()
            calls.clear()
            screen = SetupScreen(models=models, list_models_fn=blocked)
            app.push_screen(screen)
            await pilot.pause()
            # Compose + mount finished; worker is blocked on the gate, so the
            # Select must still have no real model ids -- proving compose did not fetch.
            select = screen.query_one("#brain-model-list", Select)
            blank = {None, Select.BLANK, getattr(Select, "NULL", object())}
            values = [v for _, v in select._options if v not in blank]
            assert values == []
            # Give the thread worker a moment to enter blocked().
            for _ in range(30):
                if calls:
                    break
                await pilot.pause()
            assert calls  # worker has started (off UI thread)
            gate.set()
            for _ in range(30):
                await pilot.pause()
                values = [v for _, v in select._options if v not in blank]
                if values:
                    break
            assert values == ["deepseek-v4-flash"]

    asyncio.run(main())


def test_stream_mirror_is_capped(tmp_path):
    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            for i in range(STREAM_MAX_LINES + 80):
                app._push_row(f"line-{i}")
            assert len(app._stream_rendered) <= STREAM_MAX_LINES
            # Newest lines survive the trim.
            plain = [
                (r.plain if hasattr(r, "plain") else str(r)) for r in app._stream_rendered
            ]
            assert any(f"line-{STREAM_MAX_LINES + 79}" == p for p in plain)

    asyncio.run(main())


def test_scroll_pin_skips_follow_when_not_at_bottom(tmp_path):
    async def main():
        app = _app(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            # Force working view so #stream is visible/scrollable.
            app._show_working()
            await pilot.pause()
            stream = app.query_one("#stream", VerticalScroll)
            for i in range(60):
                app._push_row(f"row-{i}")
            await pilot.pause()
            stream.scroll_home(animate=False)
            await pilot.pause()
            assert stream_should_follow(stream) is False
            before = stream.scroll_y
            app._push_row("new-while-reading")
            await pilot.pause()
            # Must not yank the reader back to the live edge.
            assert stream.scroll_y == before or abs(stream.scroll_y - before) < 1
            assert not stream.is_vertical_scroll_end

    asyncio.run(main())


def test_doctor_opens_via_worker(tmp_path):
    rows = [
        {"role": "brain", "provider": "openrouter", "model": "x",
         "status": "OK", "note": "ok"},
    ]

    async def main():
        app = _app(tmp_path, doctor_fn=lambda: rows)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._cmd_doctor()
            # Worker thread + StateChanged delivery may need a couple ticks.
            for _ in range(20):
                await pilot.pause()
                if isinstance(app.screen, SelectDialog):
                    break
            assert isinstance(app.screen, SelectDialog)
            blob = "\n".join(o["title"] for o in app.screen._options)
            assert "brain" in blob and "OK" in blob

    asyncio.run(main())


def test_model_inline_args_persist(tmp_path, monkeypatch):
    import relay.store as store

    monkeypatch.setenv("RELAY_CONFIG_DIR", str(tmp_path / "cfg"))
    cfg = ModelConfig(brain="anthropic/x", hands="deepseek-v4-flash", hands_provider="deepseek")

    async def main():
        app = _app(tmp_path, models=cfg)
        async with app.run_test() as pilot:
            await pilot.pause()
            from textual.widgets import Input

            prompt = app.query_one("#prompt", Input)
            prompt.value = "/model hands deepseek-v4-pro"
            await pilot.press("enter")
            await pilot.pause()
            saved = store.load_config()["roles"]["hands"]
            assert saved["model"] == "deepseek-v4-pro"
            assert saved["provider"] == "deepseek"

    asyncio.run(main())


def test_command_accepts_args_flag():
    queue = command_by_name("queue")
    model = command_by_name("model")
    help_cmd = command_by_name("help")
    assert queue is not None and queue.accepts_args is True
    assert model is not None and model.accepts_args is True
    assert help_cmd is not None and help_cmd.accepts_args is False
    assert {c.name for c in COMMANDS if c.accepts_args} >= {
        "queue", "redirect", "model", "cwd", "assume", "profile",
    }


def test_trim_deque_list():
    items = list(range(10))
    trim_deque_list(items, 4)
    assert items == [6, 7, 8, 9]
