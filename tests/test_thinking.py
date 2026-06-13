"""Network-free tests for the per-role DeepSeek thinking toggle.

Thinking is OFF by default and opt-in per role. When off, no thinking param is
sent; when on, ``extra_body={"thinking": {"type": "enabled"}}`` is sent,
temperature/top_p are dropped (silently ignored in thinking mode), and Relay
still parses ``content`` (its text protocol), not ``reasoning_content``.
"""

from __future__ import annotations

from types import SimpleNamespace

from relay.config import ModelConfig
from relay.models import call_model


class _CapturingClient:
    def __init__(self):
        self.calls: list[dict] = []
        client = self

        class _Completions:
            def create(self, **kwargs):
                client.calls.append(kwargs)
                # A DeepSeek thinking response would carry reasoning_content too;
                # Relay must read `content` for its protocol, never reasoning_content.
                message = SimpleNamespace(content="PROTOCOL_TEXT", reasoning_content="hidden thoughts")
                usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2)
                return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)

        self.chat = SimpleNamespace(completions=_Completions())


def _cfg(thinking: bool):
    return ModelConfig(
        brain="x", hands="deepseek-v4-pro",
        hands_provider="deepseek", hands_thinking=thinking,
    )


def test_thinking_off_by_default_sends_no_thinking_param():
    client = _CapturingClient()
    call_model("hands", [{"role": "user", "content": "hi"}], models=_cfg(False), client=client)
    sent = client.calls[0]
    assert "thinking" not in (sent.get("extra_body") or {})


def test_thinking_on_sends_enabled_and_reads_content():
    client = _CapturingClient()
    result = call_model(
        "hands", [{"role": "user", "content": "hi"}],
        models=_cfg(True), client=client, temperature=0.7, top_p=0.9,
    )
    sent = client.calls[0]
    assert sent["extra_body"]["thinking"] == {"type": "enabled"}
    # temperature/top_p are silently ignored in thinking mode -> not sent.
    assert "temperature" not in sent
    assert "top_p" not in sent
    # The protocol reads `content`, not `reasoning_content`.
    assert result.text == "PROTOCOL_TEXT"


def test_thinking_default_off_via_load_models(monkeypatch):
    monkeypatch.delenv("RELAY_HANDS_THINKING", raising=False)
    monkeypatch.delenv("RELAY_BRAIN_THINKING", raising=False)
    from relay.config import load_models

    cfg = load_models()
    assert cfg.thinking_for_role("brain") is False
    assert cfg.thinking_for_role("hands") is False
