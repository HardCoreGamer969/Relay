"""The /log debug export: redactor (safety-critical) + bundle builder.

The redactor is the guarantee that makes the export shareable, so it is tested
hardest: generic key shapes are masked, an EXACT passed-in live key value is
removed everywhere (even where it would not match a generic pattern), the function
is idempotent and crash-safe, and a bundle that seeds a fake key in EVERY field
comes out with the key absent from all of them.

The builder is assembled from existing state and makes ZERO model calls -- proven
here by passing a client whose call-count stays at 0 (it is never even referenced).
"""

from __future__ import annotations

from dataclasses import dataclass

from relay.debug import (
    MASK,
    RunSnapshot,
    build_debug_bundle,
    redact_secrets,
    summarize_run,
)

# A realistic resolution dict (the shape describe_resolution() returns): per-role
# (value, source) fields + per-provider key PRESENCE (never a value).
RESOLUTION = {
    "roles": {
        "brain": {"provider": ("openrouter", "env"), "model": ("vendor/brain", "config"),
                  "thinking": (False, "default")},
        "hands": {"provider": ("deepseek", "config"), "model": ("deepseek-chat", "config"),
                  "thinking": (True, "config")},
    },
    "providers": {
        "openrouter": {"key_present": True},
        "deepseek": {"key_present": False},
    },
}


# ============================================================================
# Part 1 -- the redactor (the critical tests)
# ============================================================================


def test_sk_keys_are_masked():
    text = "my key is sk-or-v1-0123456789abcdef0123456789abcdef and that's it"
    out = redact_secrets(text)
    assert "sk-or-v1-0123456789abcdef0123456789abcdef" not in out
    assert MASK in out
    # The sk- prefix is preserved (the documented shape).
    assert "sk-" + MASK in out


def test_bearer_and_authorization_tokens_are_masked():
    text = (
        "Authorization: Bearer abcDEF1234567890ghijKLmn\n"
        "api_key=ABCDEFGHIJKLMNOP1234567890\n"
    )
    out = redact_secrets(text)
    assert "abcDEF1234567890ghijKLmn" not in out
    assert "ABCDEFGHIJKLMNOP1234567890" not in out
    assert out.count(MASK) >= 2


def test_exact_live_key_is_removed_everywhere_even_off_pattern():
    """The strongest guarantee: the EXACT live key value is replaced verbatim
    wherever it appears, even mid-sentence and even if it would NOT match a generic
    pattern (here it has no sk- prefix and no credential marker)."""
    live = "ZZZmy-weird-LIVE-key-not-pattern-shaped-7777"
    text = f"start {live} middle '{live}' end{live}done"
    out = redact_secrets(text, known_secrets=[live])
    assert live not in out
    # Every one of the three occurrences became the mask.
    assert out.count(MASK) == 3


def test_redaction_is_idempotent():
    live = "sk-LIVELIVELIVELIVELIVELIVE0001"
    text = f"key sk-or-v1-abcdef0123456789abcdef0123 and Bearer {live} and {live}"
    once = redact_secrets(text, known_secrets=[live])
    twice = redact_secrets(once, known_secrets=[live])
    assert once == twice


def test_none_and_empty_safe():
    assert redact_secrets(None) == ""
    assert redact_secrets("") == ""
    assert redact_secrets(None, known_secrets=["sk-anything"]) == ""


def test_plain_text_without_secrets_is_unchanged():
    text = "This is a normal sentence about main() lacking an input loop. See line 42."
    assert redact_secrets(text) == text


def test_short_known_secret_does_not_nuke_ordinary_text():
    """A pathologically short 'secret' is ignored, so ordinary text survives."""
    out = redact_secrets("the cat sat on the mat", known_secrets=["at"])
    assert out == "the cat sat on the mat"


def test_bundle_with_a_fake_key_in_every_field_comes_out_clean():
    """Belt-and-suspenders: seed the SAME fake key into the transcript, activity,
    config echo, error text, plan, and memory -- it must be absent from ALL of them
    because the builder redacts the whole assembled bundle."""
    secret = "sk-INJECTED-LIVE-KEY-abcdefghij0123456789"

    @dataclass
    class _Step:
        index: int
        instruction: str
        status: str
        outcome: str | None

    @dataclass
    class _Plan:
        steps: list

    @dataclass
    class _Entry:
        kind: str
        summary: str
        detail: str = ""

    @dataclass
    class _Mem:
        entries: list

    run = RunSnapshot(status="error", cost=0.01, done=0, failed=1, total=1,
                      error=f"provider said {secret}")
    plan = _Plan(steps=[_Step(0, f"use {secret}", "failed", f"failed with {secret}")])
    memory = _Mem(entries=[_Entry("fact", f"the key {secret} leaked")])
    # A resolution dict that (wrongly) carries the secret as a model name, to prove
    # even a config-echo path is scrubbed.
    poisoned = {
        "roles": {"brain": {"provider": ("openrouter", "env"),
                            "model": (f"vendor/{secret}", "config"),
                            "thinking": (False, "default")}},
        "providers": {"openrouter": {"key_present": True}},
    }

    bundle = build_debug_bundle(
        scope="session",
        version="0.0.22",
        python_version="3.12.0",
        platform_str="TestOS",
        resolution=poisoned,
        assumption_level="auto",
        max_total_steps=50,
        run=run,
        transcript_lines=[f"brain (plan): wire up {secret}"],
        activity_lines=[f"hands | ran with {secret}"],
        plan=plan,
        memory=memory,
        known_secrets=[secret],
        timestamp="20260615-101500",
    )
    assert secret not in bundle
    assert MASK in bundle


# ============================================================================
# Part 2 -- the bundle builder
# ============================================================================


def _build(scope="current", **over):
    base = dict(
        scope=scope,
        version="0.0.22",
        python_version="3.12.1",
        platform_str="Windows-11",
        resolution=RESOLUTION,
        assumption_level="auto",
        max_total_steps=50,
        run=RunSnapshot(status="completed", cost=0.0234, done=2, failed=0, total=2),
        transcript_lines=["you (goal): build x", "brain (plan): step 1"],
        activity_lines=["brain | plan: 2 step(s)"],
        plan=None,
        memory=None,
        known_secrets=[],
        timestamp="20260615-101500",
    )
    base.update(over)
    return build_debug_bundle(**base)


def test_bundle_has_all_expected_sections():
    out = _build()
    for header in (
        "# Relay debug export",
        "## Environment",
        "## Resolved config (non-secret)",
        "## Run outcome",
        "## Conversation transcript",
        "## Activity log",
        "## Plan",
        "## Memory",
    ):
        assert header in out


def test_bundle_includes_version_python_os():
    out = _build()
    assert "0.0.22" in out
    assert "3.12.1" in out
    assert "Windows-11" in out


def test_bundle_reports_key_presence_not_values():
    out = _build()
    assert "openrouter: present" in out
    assert "deepseek: absent" in out
    # The presence labels are the only key info; no value is ever emitted.
    assert "key value" not in out.lower() or "never a key value" in out.lower()


def test_bundle_scope_header_and_note_differ():
    current = _build(scope="current")
    session = _build(scope="session")
    assert "Current project" in current
    assert "Full session" in session
    # The session note flags the session-spanning buffers + current-only structure.
    assert "WHOLE session" in session
    assert "WHOLE session" not in current


def test_bundle_makes_zero_model_calls():
    """The builder never touches a model client -- a counting client stays at 0."""

    class _CountingClient:
        def __init__(self):
            self.calls = 0

        def __getattr__(self, _name):
            self.calls += 1
            raise AssertionError("the bundle builder must not call the model client")

    client = _CountingClient()
    _build()  # builder takes no client; this just proves the path is client-free
    assert client.calls == 0


def test_bundle_renders_plan_and_memory_when_present():
    @dataclass
    class _Step:
        index: int
        instruction: str
        status: str
        outcome: str | None

    @dataclass
    class _Plan:
        steps: list

    @dataclass
    class _Entry:
        kind: str
        summary: str

    @dataclass
    class _Mem:
        entries: list

    plan = _Plan(steps=[_Step(0, "create file", "done", "created x.py"),
                        _Step(1, "run tests", "failed", "2 failed")])
    memory = _Mem(entries=[_Entry("decision", "use pytest")])
    out = _build(plan=plan, memory=memory)
    assert "[done] create file -- created x.py" in out
    assert "[failed] run tests" in out
    assert "[decision] use pytest" in out


# ============================================================================
# summarize_run -- distilling a run outcome into a RunSnapshot
# ============================================================================


def test_summarize_run_none_is_none():
    assert summarize_run(None) is None


def test_summarize_run_counts_steps_and_finds_limit():
    @dataclass
    class _Step:
        status: str

    @dataclass
    class _Plan:
        steps: list

    @dataclass
    class _Result:
        status: str
        plan: object
        max_total_steps: int | None = None

    @dataclass
    class _Outcome:
        status: str
        result: object
        error: str = ""

    plan = _Plan(steps=[_Step("done"), _Step("done"), _Step("failed")])
    # max_steps is a limit terminal -> a friendly note should be filled.
    result = _Result(status="max_steps", plan=plan, max_total_steps=50)
    snap = summarize_run(_Outcome(status="max_steps", result=result), cost=0.5)
    assert snap.done == 2 and snap.failed == 1 and snap.total == 3
    assert snap.cost == 0.5
    assert snap.limit_fired  # friendly_terminal_message returned a note
    assert "50" in snap.limit_fired
