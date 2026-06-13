"""Regression tests for .env loading from the current working directory.

The shipped bug: a bare ``load_dotenv()`` resolves its search relative to the
*caller module's file* (python-dotenv's ``usecwd=False`` default), so under a
global/editable install Relay searched its own install tree and silently ignored a
project ``.env`` in the user's working directory. The fix keys the search off
``os.getcwd()`` (``find_dotenv(usecwd=True)``).

These tests are network-free and exercise the real cwd via ``monkeypatch.chdir``.
The earlier suite passed while the bug shipped precisely because nothing exercised
a real cwd ``.env`` -- that gap is closed here.
"""

from __future__ import annotations

import os

from relay.config import DEFAULT_HANDS_MODEL, load_env, load_models
from relay.providers import DEFAULT_PROVIDER


def _same_dir(a: str, b: str) -> bool:
    return os.path.samefile(a, b)


def test_env_file_in_cwd_is_loaded(monkeypatch, tmp_path):
    """THE regression: a .env in the working directory must take effect.

    Fails on the old behavior (cwd ignored), passes on the fix.
    """
    monkeypatch.delenv("RELAY_HANDS_PROVIDER", raising=False)
    monkeypatch.delenv("RELAY_HANDS_MODEL", raising=False)
    (tmp_path / ".env").write_text(
        "RELAY_HANDS_PROVIDER=deepseek\nRELAY_HANDS_MODEL=deepseek-v4-flash\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    cfg = load_models()
    assert cfg.provider_for_role("hands") == "deepseek"
    assert cfg.for_role("hands") == "deepseek-v4-flash"


def test_process_env_overrides_env_file(monkeypatch, tmp_path):
    """Conventional precedence: a real process env var beats the .env file value,
    so the session-var workflow the user relied on keeps working."""
    (tmp_path / ".env").write_text("RELAY_HANDS_MODEL=from-file\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RELAY_HANDS_MODEL", "from-process-env")

    cfg = load_models()
    assert cfg.for_role("hands") == "from-process-env"  # env wins, not the file


def test_absent_env_file_is_harmless(monkeypatch, tmp_path):
    """No .env present: no error, and env vars/defaults apply as before."""
    monkeypatch.delenv("RELAY_HANDS_PROVIDER", raising=False)
    monkeypatch.delenv("RELAY_HANDS_MODEL", raising=False)
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)

    assert load_env() == ""  # nothing found, returns cleanly
    cfg = load_models()  # falls back to defaults, no exception
    assert cfg.provider_for_role("hands") == DEFAULT_PROVIDER
    assert cfg.for_role("hands") == DEFAULT_HANDS_MODEL


def test_load_keys_off_cwd_not_install_location(monkeypatch, tmp_path):
    """Install-location independence: the .env found is the one in cwd, NOT one in
    Relay's module/install tree. This is the heart of the original failure."""
    import relay

    project = tmp_path / "some_project"
    project.mkdir()
    (project / ".env").write_text("RELAY_BRAIN_MODEL=cwd-marker-model\n", encoding="utf-8")
    monkeypatch.delenv("RELAY_BRAIN_MODEL", raising=False)
    monkeypatch.chdir(project)

    loaded = load_env()
    assert loaded  # a path was found
    assert loaded.endswith(".env")
    # It is the cwd's .env, not one inside the installed relay package.
    assert _same_dir(os.path.dirname(loaded), str(project))
    relay_dir = os.path.dirname(relay.__file__)
    assert not _same_dir(os.path.dirname(loaded), relay_dir)

    assert load_models().for_role("brain") == "cwd-marker-model"
