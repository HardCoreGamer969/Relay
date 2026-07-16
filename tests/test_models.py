"""Network-free tests for the model seam.

The OpenRouter client's ``chat.completions.create`` is mocked — no real calls
are ever made. A fake response exposes ``.choices[0].message.content`` and a
``.usage`` block (tokens plus OpenRouter's returned cost).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from relay.config import ModelConfig
from relay.models import call_model
from relay.telemetry import Ledger


def make_response(content, prompt_tokens, completion_tokens, cost):
    """Build a fake OpenAI/OpenRouter-shaped response object."""
    usage = SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        cost=cost,
    )
    message = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice], usage=usage)


class FakeCompletions:
    def __init__(self, response):
        self.response = response
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class FakeClient:
    """Stand-in for ``openai.OpenAI`` — never hits the network."""

    def __init__(self, response):
        self.chat = SimpleNamespace(completions=FakeCompletions(response))


class PydanticStyleUsage:
    """Mimics a pydantic v2 usage model (``extra="allow"``), as the openai SDK
    builds it: OpenRouter's extra ``cost`` lives in ``model_extra``, NOT as a
    first-class attribute. This is the real response shape the v0.01 test missed.
    """

    def __init__(self, prompt_tokens, completion_tokens, extra):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = prompt_tokens + completion_tokens
        self.model_extra = dict(extra)
        # deliberately NO top-level ``cost`` attribute


def test_role_resolves_to_right_model_slug():
    cfg = ModelConfig(brain="vendor/brain-model", hands="vendor/hands-model")
    assert cfg.for_role("brain") == "vendor/brain-model"
    assert cfg.for_role("hands") == "vendor/hands-model"
    assert cfg.for_role("hands-2") == "vendor/hands-model"
    assert ModelConfig.canonical_role("hands-3") == "hands"
    with pytest.raises(ValueError):
        cfg.for_role("unknown-role")


def test_single_call_records_one_entry_with_tokens_and_cost():
    cfg = ModelConfig(brain="vendor/brain-model", hands="vendor/hands-model")
    ledger = Ledger()
    client = FakeClient(make_response("one concrete step", 10, 5, 0.000123))

    result = call_model(
        "brain",
        [{"role": "user", "content": "hi"}],
        models=cfg,
        ledger=ledger,
        client=client,
    )

    # Returned text and a single recorded telemetry entry.
    assert result.text == "one concrete step"
    assert len(ledger.records) == 1

    rec = ledger.records[0]
    assert rec.role == "brain"
    assert rec.model == "vendor/brain-model"
    assert rec.prompt_tokens == 10
    assert rec.completion_tokens == 5
    assert rec.total_tokens == 15
    assert rec.cost_usd == 0.000123

    # The seam must request OpenRouter's per-generation cost.
    sent = client.chat.completions.calls[0]
    assert sent["model"] == "vendor/brain-model"
    assert sent["extra_body"]["usage"]["include"] is True


def test_brain_then_hands_yields_by_role_with_two_distinct_models():
    cfg = ModelConfig(brain="vendor/brain-model", hands="vendor/hands-model")
    ledger = Ledger()
    client = FakeClient(make_response("text", 3, 4, 0.0002))

    call_model("brain", [{"role": "user", "content": "a"}], models=cfg, ledger=ledger, client=client)
    call_model("hands", [{"role": "user", "content": "b"}], models=cfg, ledger=ledger, client=client)

    summary = ledger.by_role()
    assert set(summary) == {"brain", "hands"}
    assert summary["brain"].model == "vendor/brain-model"
    assert summary["hands"].model == "vendor/hands-model"

    models_used = {s.model for s in summary.values()}
    assert len(models_used) == 2, "brain and hands must be two distinct models"

    # Aggregated totals.
    assert ledger.total_cost() == pytest.approx(0.0004)
    assert summary["brain"].total_tokens == 7


def test_missing_cost_falls_back_to_none():
    cfg = ModelConfig(brain="vendor/brain-model", hands="vendor/hands-model")
    ledger = Ledger()
    client = FakeClient(make_response("step", 2, 1, None))

    call_model("brain", [{"role": "user", "content": "x"}], models=cfg, ledger=ledger, client=client)

    assert ledger.records[0].cost_usd is None
    assert ledger.total_cost() is None


def test_cost_is_read_from_model_extra_when_no_top_level_attribute():
    """Locks in the REAL response shape: cost only in pydantic's model_extra."""
    cfg = ModelConfig(brain="vendor/brain-model", hands="vendor/hands-model")
    ledger = Ledger()

    usage = PydanticStyleUsage(prompt_tokens=7, completion_tokens=3, extra={"cost": 0.00042})
    # Sanity: there is genuinely no top-level .cost attribute to read.
    assert getattr(usage, "cost", None) is None

    message = SimpleNamespace(content="ok")
    response = SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)
    client = FakeClient(response)

    result = call_model(
        "brain", [{"role": "user", "content": "hi"}], models=cfg, ledger=ledger, client=client
    )

    assert result.record.prompt_tokens == 7
    assert result.record.completion_tokens == 3
    assert result.record.cost_usd == pytest.approx(0.00042)
    assert ledger.total_cost() == pytest.approx(0.00042)


def test_empty_choices_returns_empty_text_not_crash():
    """A provider returning an empty choices array (content filter, safety refusal,
    rate-limit-with-empty-body) must not crash the run -- return empty text so the
    loop's parse-failure nudge handles it gracefully."""
    cfg = ModelConfig(brain="vendor/brain-model", hands="vendor/hands-model")
    ledger = Ledger()
    usage = SimpleNamespace(prompt_tokens=5, completion_tokens=0, total_tokens=5, cost=None)
    response = SimpleNamespace(choices=[], usage=usage)
    client = FakeClient(response)

    result = call_model(
        "brain", [{"role": "user", "content": "hi"}], models=cfg, ledger=ledger, client=client
    )

    assert result.text == ""  # not an IndexError -- a recoverable empty turn
    assert result.record.prompt_tokens == 5
    assert result.record.completion_tokens == 0


# --- retry with backoff for transient API failures (v0.0.32) -----------------


class _RetriableError(Exception):
    """Mimics an openai APIStatusError with a status_code attribute."""
    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__(f"HTTP {status_code}")


class _NonRetriableError(Exception):
    """Mimics a 400 Bad Request (not retriable)."""
    def __init__(self, status_code=400):
        self.status_code = status_code
        super().__init__(f"HTTP {status_code}")


class _RetryingCompletions:
    """Raises a retriable error N times, then succeeds (or raises forever)."""

    def __init__(self, fail_times, error_cls=_RetriableError, status=429):
        self.fail_times = fail_times
        self.error_cls = error_cls
        self.status = status
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.error_cls(self.status)
        return make_response("ok", 5, 5, 0.0001)


class _RetryingClient:
    def __init__(self, fail_times, error_cls=_RetriableError, status=429):
        self.chat = SimpleNamespace(completions=_RetryingCompletions(fail_times, error_cls, status))

    @property
    def calls(self):
        return self.chat.completions.calls


def test_retries_on_429_then_succeeds(monkeypatch):
    """A 429 (rate limit) is retried; the second attempt succeeds."""
    monkeypatch.setattr("relay.models.time.sleep", lambda s: None)  # no real sleep
    cfg = ModelConfig(brain="vendor/brain-model", hands="vendor/hands-model")
    client = _RetryingClient(fail_times=1, status=429)

    result = call_model(
        "brain", [{"role": "user", "content": "hi"}], models=cfg, ledger=Ledger(), client=client
    )

    assert result.text == "ok"
    assert client.calls == 2  # 1 failure + 1 success


def test_retries_on_503_then_succeeds(monkeypatch):
    """A 503 (service unavailable) is also retried."""
    monkeypatch.setattr("relay.models.time.sleep", lambda s: None)
    cfg = ModelConfig(brain="vendor/brain-model", hands="vendor/hands-model")
    client = _RetryingClient(fail_times=1, status=503)

    result = call_model(
        "brain", [{"role": "user", "content": "hi"}], models=cfg, ledger=Ledger(), client=client
    )

    assert result.text == "ok"
    assert client.calls == 2


def test_does_not_retry_on_400(monkeypatch):
    """A 400 (bad request) is NOT retried -- it propagates immediately."""
    monkeypatch.setattr("relay.models.time.sleep", lambda s: None)
    cfg = ModelConfig(brain="vendor/brain-model", hands="vendor/hands-model")
    client = _RetryingClient(fail_times=5, error_cls=_NonRetriableError, status=400)

    with pytest.raises(_NonRetriableError):
        call_model(
            "brain", [{"role": "user", "content": "hi"}], models=cfg, ledger=Ledger(), client=client
        )
    assert client.calls == 1  # no retry -- propagated immediately


def test_exhausts_retries_then_propagates(monkeypatch):
    """If all retries are exhausted, the error propagates after the last attempt."""
    monkeypatch.setattr("relay.models.time.sleep", lambda s: None)
    monkeypatch.setenv("RELAY_MAX_RETRIES", "2")
    cfg = ModelConfig(brain="vendor/brain-model", hands="vendor/hands-model")
    # Always fails (fail_times=10 >> retries=2)
    client = _RetryingClient(fail_times=10, status=429)

    with pytest.raises(_RetriableError):
        call_model(
            "brain", [{"role": "user", "content": "hi"}], models=cfg, ledger=Ledger(), client=client
        )
    assert client.calls == 3  # 1 initial + 2 retries = 3 total attempts


# --- v0.0.32: connection-error retry + Retry-After + jitter + timeout --------


def test_retries_on_api_connection_error(monkeypatch):
    """An openai.APIConnectionError (TCP reset / DNS failure / TLS handshake)
    is retried -- the v0.0.31 fix: a single transient network blip used to
    propagate and abort the whole run."""
    from openai import APIConnectionError
    monkeypatch.setattr("relay.models.time.sleep", lambda s: None)
    cfg = ModelConfig(brain="vendor/brain-model", hands="vendor/hands-model")

    # APIConnectionError takes a single ``request`` arg; we build a stub
    # that quacks like an httpx Request for the constructor.
    class _StubCompletions:
        def __init__(self):
            self.calls = 0

        def create(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise APIConnectionError(request=None)
            return make_response("ok", 5, 5, 0.0001)

    class _StubClient:
        def __init__(self):
            self.chat = SimpleNamespace(completions=_StubCompletions())

    result = call_model(
        "brain", [{"role": "user", "content": "hi"}], models=cfg, ledger=Ledger(), client=_StubClient()
    )
    assert result.text == "ok"
    # (2 calls = 1 connection error + 1 success)


def test_retries_on_api_timeout_error(monkeypatch):
    """An openai.APITimeoutError (a request timed out) is also retried --
    the v0.0.32 explicit ``timeout`` kwarg makes timeouts surface as
    APITimeoutError instead of hanging, and we treat it as retriable."""
    from openai import APITimeoutError
    monkeypatch.setattr("relay.models.time.sleep", lambda s: None)
    cfg = ModelConfig(brain="vendor/brain-model", hands="vendor/hands-model")
    client = _RetryingClient(fail_times=1, error_cls=APITimeoutError, status=None)

    result = call_model(
        "brain", [{"role": "user", "content": "hi"}], models=cfg, ledger=Ledger(), client=client
    )
    assert result.text == "ok"
    assert client.calls == 2


def test_honors_retry_after_header(monkeypatch):
    """When the provider's ``Retry-After`` header is present, its value (in
    seconds) is used for the sleep instead of the exponential-backoff
    fallback. The v0.0.32 fix -- the openai SDK ignores Retry-After itself."""
    sleeps: list[float] = []

    class _RespWithHeader:
        status_code = 429
        headers = {"Retry-After": "7"}

        def __init__(self):
            self.status_code = 429
            self.headers = {"Retry-After": "7"}

    class _ErrorWithHeader(Exception):
        def __init__(self):
            super().__init__("HTTP 429")
            self.status_code = 429
            self.response = _RespWithHeader()

    class _Completions:
        def __init__(self):
            self.calls = 0

        def create(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise _ErrorWithHeader()
            return make_response("ok", 5, 5, 0.0001)

    class _Client:
        def __init__(self):
            self.chat = SimpleNamespace(completions=_Completions())

    def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr("relay.models.time.sleep", fake_sleep)
    cfg = ModelConfig(brain="vendor/brain-model", hands="vendor/hands-model")
    client = _Client()
    result = call_model(
        "brain", [{"role": "user", "content": "hi"}], models=cfg, ledger=Ledger(), client=client
    )
    assert result.text == "ok"
    assert client.chat.completions.calls == 2
    assert sleeps == [7.0]  # Retry-After honored, not the exponential fallback


def test_exponential_backoff_with_jitter(monkeypatch):
    """When no Retry-After is present, the sleep follows exponential backoff
    with jitter: base = 0.5 * 2**attempt, capped at 30s, plus uniform jitter
    in [0, base). On attempt 0, base = 0.5; on attempt 1, base = 1.0. The
    test asserts the sleep is within [base, 2*base)."""
    sleeps: list[float] = []

    def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr("relay.models.time.sleep", fake_sleep)
    monkeypatch.setenv("RELAY_MAX_RETRIES", "2")
    cfg = ModelConfig(brain="vendor/brain-model", hands="vendor/hands-model")
    client = _RetryingClient(fail_times=2, status=503)

    result = call_model(
        "brain", [{"role": "user", "content": "hi"}], models=cfg, ledger=Ledger(), client=client
    )
    assert result.text == "ok"
    assert len(sleeps) == 2
    # attempt 0: base=0.5, sleep in [0.5, 1.0)
    assert 0.5 <= sleeps[0] < 1.0, f"first sleep {sleeps[0]} not in [0.5, 1.0)"
    # attempt 1: base=1.0, sleep in [1.0, 2.0)
    assert 1.0 <= sleeps[1] < 2.0, f"second sleep {sleeps[1]} not in [1.0, 2.0)"


def test_request_timeout_is_passed_explicitly(monkeypatch):
    """The explicit ``timeout`` kwarg is forwarded to ``client.chat.completions.create``
    so a hung provider can't stall a step on the SDK's 600s default. We
    pin it to 120s here to prove the kwarg flows through."""
    seen: dict = {}

    class _Completions:
        def create(self, **kwargs):
            seen.update(kwargs)
            return make_response("ok", 5, 5, 0.0001)

    class _Client:
        def __init__(self):
            self.chat = SimpleNamespace(completions=_Completions())

    monkeypatch.setenv("RELAY_REQUEST_TIMEOUT_S", "120")
    cfg = ModelConfig(brain="vendor/brain-model", hands="vendor/hands-model")
    call_model(
        "brain", [{"role": "user", "content": "hi"}], models=cfg, ledger=Ledger(), client=_Client()
    )
    assert seen.get("timeout") == 120.0


def test_request_timeout_default_is_explicit_and_bounded(monkeypatch):
    """Without an override, Relay avoids the SDK's historical 600s stall."""
    seen: dict = {}

    class _Completions:
        def create(self, **kwargs):
            seen.update(kwargs)
            return make_response("ok", 5, 5, 0.0001)

    class _Client:
        def __init__(self):
            self.chat = SimpleNamespace(completions=_Completions())

    monkeypatch.delenv("RELAY_REQUEST_TIMEOUT_S", raising=False)
    cfg = ModelConfig(brain="vendor/brain-model", hands="vendor/hands-model")
    call_model(
        "brain", [{"role": "user", "content": "hi"}], models=cfg, ledger=Ledger(), client=_Client()
    )
    assert "timeout" in seen
    assert seen["timeout"] == 120.0
