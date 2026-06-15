"""Bug 5 (v0.0.19): raw provider/API JSON must never reach the user.

A beta user must never see a blob like ``Error code: 400 - {'error': {... 'raw': ...}}``.
Every point a provider error would surface -- the run-error path and the slash live
calls (validation/listing/doctor) -- renders a friendly, ASCII-safe message: what
failed, which provider/model, and a short hint to re-pick. The raw JSON is dropped
(it may be logged at debug, never shown). Clean notes pass through unchanged.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from relay.bridge import RunOutcome, STATUS_ERROR
from relay.config import ModelConfig
from relay.tui import RelayTuiApp, friendly_provider_error

# A representative raw provider error: HTTP 400 with a nested `raw` JSON payload.
RAW_400 = (
    "Error code: 400 - {'error': {'message': 'Provider returned error', "
    "'code': 400, 'metadata': {'raw': '{\"error\":\"bad model\"}'}}}"
)


def _app(tmp_path, *, validate_fn=None):
    return RelayTuiApp(
        root=str(tmp_path), models=ModelConfig(brain="vendor/brain", hands="vendor/hands"),
        client=SimpleNamespace(),
        validate_fn=validate_fn or (lambda provider, model: (True, "ok")),
    )


def _has_raw(text: str) -> bool:
    return "{'error'" in text or "'raw'" in text or "Error code:" in text


# --- the run-error path ------------------------------------------------------


def test_run_error_renders_friendly_not_raw_json(tmp_path):
    async def main():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._runner = None  # _handle_finished tolerates a None runner
            app._handle_finished(RunOutcome(status=STATUS_ERROR, error=f"BadRequestError: {RAW_400}"))
            joined = "\n".join(app._conversation_lines)
            assert not _has_raw(joined), f"raw JSON leaked: {joined!r}"
            # ...and it still says something useful: a hint to re-pick.
            assert "/model" in joined or "/provider" in joined or "/doctor" in joined

    asyncio.run(main())


# --- the slash live-call (validation) path -----------------------------------


def test_slash_validation_error_is_friendly(tmp_path):
    async def main():
        app = _app(tmp_path, validate_fn=lambda provider, model: (False, RAW_400))
        async with app.run_test() as pilot:
            await pilot.pause()
            ok, note = app._save_role_model("brain", "openrouter", "bad/slug")
            assert ok is False
            assert not _has_raw(note), f"raw JSON leaked into the note: {note!r}"
            assert "openrouter" in note.lower()      # which provider
            assert "bad/slug" in note                # which model
            assert "/model" in note or "/provider" in note  # a hint

    asyncio.run(main())


# --- the helper itself: friendly only for raw blobs, pass-through otherwise ---


def test_friendly_provider_error_rewrites_raw_blob():
    msg = friendly_provider_error(RAW_400, provider="deepseek", model="x/y")
    assert not _has_raw(msg)
    assert "DeepSeek" in msg and "x/y" in msg
    assert "/model" in msg or "/provider" in msg


def test_friendly_provider_error_passes_clean_notes_through():
    """A clean validation note is NOT a raw blob -- it must survive unchanged (no
    false-positive rewriting that would erase a perfectly good message)."""
    clean = "'typo/nope' is not in deepseek's live model list"
    assert friendly_provider_error(clean, provider="deepseek", model="typo/nope") == clean
