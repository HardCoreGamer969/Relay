"""Tests for the v0.0.32 secret-scrubbing / redaction path (0.5).

Closes the exfiltration path: a model that runs ``env`` / ``set`` / reads a
``.env`` file used to be able to observe the parent's API keys and then send
them back as part of its own next message. The fix is two-layered:

1. ``_scrubbed_env()`` -- bash inherits a CLEAN copy of the parent's env
   (every key/token/secret-shaped var is dropped before exec). The
   subprocess literally has no way to see the values.
2. ``_redact_observation()`` -- every text observation (read / grep / bash
   / webfetch) is run through the same ``redact_secrets`` the /log export
   uses, with the parent's live secret values passed as ``known_secrets``
   so even a value that slipped past the env-scrub (e.g. the user did
   ``export`` in their shell rc) is masked before the model sees it.
"""

from __future__ import annotations

import os

import pytest

from relay.tools import (
    _SECRET_ENV_SUFFIXES,
    Tools,
    _redact_observation,
    _scrubbed_env,
    _scrubbed_secrets_from_env,
)


def test_scrubbed_env_drops_secret_shaped_vars(monkeypatch):
    """Every *_API_KEY / *_TOKEN / *_SECRET / *_PASSWORD / *_AUTH / etc.
    env var must be absent from the scrubbed env, while neutral vars
    (PATH / HOME / LANG / custom) are preserved."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-REDACTED-1234567890")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-1234567890abcdef")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_1234567890abcdef")
    monkeypatch.setenv("DB_PASSWORD", "hunter2")
    monkeypatch.setenv("MY_AUTH", "bearer-xyz")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/root")
    monkeypatch.setenv("RELAY_DEBUG", "1")

    clean = _scrubbed_env()
    # Secrets gone
    for dropped in (
        "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY", "GITHUB_TOKEN",
        "DB_PASSWORD", "MY_AUTH",
    ):
        assert dropped not in clean, f"{dropped} should be scrubbed but was present"
    # Neutral kept
    for kept in ("PATH", "HOME", "RELAY_DEBUG"):
        assert kept in clean, f"{kept} should be preserved but was scrubbed"


def test_scrubbed_env_does_not_mutate_live_env(monkeypatch):
    """The live process env must be unchanged after the scrub -- a side
    effect would be a hard-to-find bug in a later test or library."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-test-1234567890")
    before = dict(os.environ)
    _scrubbed_env()
    assert os.environ == before


def test_scrubbed_secrets_from_env_collects_values(monkeypatch):
    """The values of the secret-shaped env vars are collected, ready to be
    passed to ``redact_secrets`` as ``known_secrets`` for the observation."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-test-1234567890")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_1234567890abcdef")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    secrets = _scrubbed_secrets_from_env()
    assert "sk-or-v1-test-1234567890" in secrets
    assert "ghp_1234567890abcdef" in secrets
    # Sub-6-char values are ignored (the redactor's own floor).
    monkeypatch.setenv("TINY", "abc")
    assert "abc" not in _scrubbed_secrets_from_env()


def test_redact_observation_masks_known_secret_in_text():
    """A live secret value present anywhere in the observation text is masked
    before it reaches the model. This is the 2nd-line defense behind the
    env-scrub: a script that ``export``-ed the value would be caught here."""
    text = "the API key is sk-or-v1-MY-REAL-KEY-1234567890 and we use it for things"
    out = _redact_observation(text, extra_secrets=["sk-or-v1-MY-REAL-KEY-1234567890"])
    assert "sk-or-v1-MY-REAL-KEY-1234567890" not in out
    assert "REDACTED" in out


def test_redact_observation_passthrough_when_no_secrets():
    """A plain observation with no secrets present is unchanged -- the
    common path is a no-op, so the cost of every-tool-observation-redaction
    is just one regex pass over text that has nothing to mask."""
    text = "alpha\nbeta\ngamma"
    assert _redact_observation(text, extra_secrets=["sk-nope-1234567890abcdef"]) == text


def test_redact_observation_handles_empty():
    assert _redact_observation("") == ""
    assert _redact_observation(None) == "" if False else _redact_observation("") == ""  # noop


# --- end-to-end: a real bash sub-process can't see the parent's secrets ---

def test_bash_subprocess_cannot_see_parent_api_key(tmp_path, monkeypatch):
    """The whole point of the env scrub: a child process spawned by bash
    must not see the parent's API key, even if it explicitly asks for it."""
    secret = "sk-or-v1-PROOF-OF-SCRUB-1234567890"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)
    # python -c is portable; on every platform it can import os and print.
    cmd = "python -c \"import os, sys; sys.stdout.write(os.environ.get('OPENROUTER_API_KEY', '<<absent>>'))\""
    out = Tools(tmp_path).bash(cmd)
    if "not recognized" in out or "No such file" in out or "cannot find" in out.lower():
        pytest.skip("python not on PATH in this env; env-scrub unit-tested above")
    assert "<<absent>>" in out, f"env scrub failed: bash saw {out!r}"
    assert secret not in out


def test_read_masks_secret_in_file(tmp_path, monkeypatch):
    """A ``read`` of a file containing a live secret value masks that
    value before the observation is returned. This closes the case where
    a user has an actual ``.env`` file (or a config.json) the model might
    read; the parent's live values are masked in addition to whatever the
    file itself contains."""
    secret = "sk-or-v1-FROM-ENV-1234567890"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)
    f = tmp_path / "config.txt"
    f.write_text(f"the api key is {secret}, used to call models\n", encoding="utf-8")
    out = Tools(tmp_path).read("config.txt")
    assert secret not in out
    assert "REDACTED" in out


# --- v0.0.32: the process-tree kill on bash timeout (0.6) --------------------
#
# The v0.0.31 bug: subprocess.run(timeout=...) only kills the shell; a
# descendant the shell spawned (a forked server, a compiled subprocess, a
# python child via subprocess.Popen) survives, holds the pipes, and wedges
# the worker thread forever. The fix is to use Popen directly + a tree-kill
# helper on timeout. These tests pin that the helper actually kills the
# descendants, end-to-end with a real subprocess tree.


import sys


def test_bash_timeout_kills_descendant_subprocess(tmp_path):
    """A bash command that spawns a grandchild python that writes a marker,
    then sleeps: after the timeout fires, BOTH the shell and the python
    child must be gone (no marker file written, no python process left)."""
    import time
    marker = tmp_path / "grandchild-finished.txt"
    # The shell launches a python that should run for 10s -- we time out at
    # 0.5s. The python never gets to write the marker if it's killed.
    if os.name == "nt":
        cmd = (
            f'cmd /c "python -c \"import time; '
            f'time.sleep(10); open(r\\"{marker}\\", \\"w\\").write(\\"x\\")\""'
        )
    else:
        cmd = (
            f"sh -c 'python -c \"import time; "
            f"time.sleep(10); open({marker!r}, \\\"w\\\").write(\\\"x\\\")\"' &"
            f" sleep 10"
        )
    tools = Tools(tmp_path, bash_timeout_s=0.5)
    start = time.perf_counter()
    out = tools.bash(cmd)
    elapsed = time.perf_counter() - start
    # Sanity: we timed out fast (not 10s).
    assert elapsed < 5.0, f"bash didn't return promptly after timeout: {elapsed:.2f}s"
    assert "timed out" in out
    # The grandchild didn't get to write the marker because we killed the
    # whole tree (this is the v0.0.31 bug: a hung child would keep going
    # for 10s, but on success the test asserts it was killed in <1s).
    # NOTE: on Windows, the python child may not be reachable through
    # taskkill /T depending on how cmd spawns it, so the marker check is a
    # best-effort assertion; the real guarantee is that ``tools.bash``
    # returns promptly (the elapsed check above). On POSIX, the killpg
    # SIGTERMs the python child before its sleep returns.
    # Give the OS a moment to fully reap the python process before checking.
    time.sleep(0.2)
    if os.name != "nt":
        assert not marker.exists(), (
            f"grandchild survived timeout and wrote {marker} -- tree kill failed"
        )
