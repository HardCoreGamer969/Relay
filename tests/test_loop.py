"""Network-free tests for the single-model agent loop.

The OpenRouter client is mocked at the ``call_model`` boundary with a scripted
sequence of model replies -- no network, no real models.
"""

from __future__ import annotations

from types import SimpleNamespace

from relay.config import ModelConfig
from relay.loop import run_task
from relay.telemetry import Ledger


def make_response(content, prompt_tokens=5, completion_tokens=5, cost=0.0001):
    usage = SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        cost=cost,
    )
    message = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice], usage=usage)


class ScriptedCompletions:
    """Returns a pre-scripted reply per call, in order."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return make_response(self._replies.pop(0))


class ScriptedClient:
    """Stand-in for ``openai.OpenAI`` driving a scripted conversation."""

    def __init__(self, replies):
        self.chat = SimpleNamespace(completions=ScriptedCompletions(replies))


CFG = ModelConfig(brain="vendor/brain", hands="vendor/hands")


def test_loop_edits_a_file_then_finishes(tmp_path):
    ledger = Ledger()
    client = ScriptedClient(
        [
            '<edit path="hello.txt">hi from relay</edit>',
            "<done>created hello.txt</done>",
        ]
    )

    result = run_task(
        "create hello.txt",
        tmp_path,
        role="hands",
        models=CFG,
        ledger=ledger,
        client=client,
    )

    assert result.done is True
    assert result.done_summary == "created hello.txt"
    assert (tmp_path / "hello.txt").read_text(encoding="utf-8") == "hi from relay"
    # Two model calls, both recorded; no parse failures on the happy path.
    assert len(ledger.records) == 2
    assert ledger.parse_failures == 0
    # Transcript holds the edit step and the done step.
    assert [s.kind for s in result.steps] == ["edit", "done"]


def test_parse_failure_is_recorded_then_recovered(tmp_path):
    ledger = Ledger()
    client = ScriptedClient(
        [
            "I'll just chat instead of using the protocol.",  # parse failure
            "<done>nothing to do</done>",
        ]
    )

    result = run_task("noop", tmp_path, models=CFG, ledger=ledger, client=client)

    assert ledger.parse_failures == 1
    assert result.done is True
    assert result.steps[0].kind == "parse_failure"


def test_loop_aborts_after_consecutive_parse_failures(tmp_path):
    ledger = Ledger()
    client = ScriptedClient(["no tags here"] * 5)

    result = run_task(
        "noop", tmp_path, models=CFG, ledger=ledger, client=client, max_steps=10
    )

    assert result.done is False
    # Aborts after 3 consecutive parse failures (only 3 model calls consumed).
    assert ledger.parse_failures == 3
    assert len(ledger.records) == 3


def test_on_step_callback_streams_each_step(tmp_path):
    ledger = Ledger()
    client = ScriptedClient(
        [
            '<edit path="x.txt">data</edit>',
            "<done>done</done>",
        ]
    )
    seen = []

    run_task(
        "x",
        tmp_path,
        models=CFG,
        ledger=ledger,
        client=client,
        on_step=lambda step: seen.append(step.kind),
    )

    assert seen == ["edit", "done"]
