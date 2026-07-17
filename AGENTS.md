# AGENTS.md

## Cursor Cloud specific instructions

Relay is a Python CLI/TUI "planner + executor" coding agent (`relay` command, distributed as `relay-cli`). It is managed with **uv** (`uv.lock` is committed). There is no backend/DB — it is a local CLI that reaches LLMs through an OpenAI-compatible API. The update script runs `uv sync --extra dev`, so dependencies (incl. `pytest`) are already installed when a session starts.

### Running tests — set `TTY_COMPATIBLE=0`
Run the suite with `./scripts/test.sh` (forwards pytest args, e.g. `./scripts/test.sh tests/test_cli.py`).

`scripts/test.sh` defaults `TTY_COMPATIBLE=0` and blocks outbound sockets (`--allow-hosts` loopback only), matching CI. For live canaries: `RELAY_ALLOW_NETWORK=1 ./scripts/test.sh tests/live -m live`.

The Cloud VM exports `TERM=dumb` and runs commands under a pty. Without `TTY_COMPATIBLE=0`, a few Rich-rendered CLI tests (`tests/test_cli.py::test_runs_command_shows_persisted_runs`, `::test_doctor_reports_catalog_status`, `::test_doctor_reports_context_window`) fail: `TERM=dumb` makes Rich clamp the console width (so explicit `Console(width=200)` is ignored), and a non-dumb pty makes Rich emit ANSI styling that breaks plain-text substring assertions. `TTY_COMPATIBLE=0` forces Rich into non-terminal mode, which both honors explicit widths and disables ANSI, matching what the suite expects. The hermetic suite is network-free (client mocked, catalog served from a local fixture).

### Running the app
Standard commands are documented in `README.md` (Usage / Develop). Quick ones that work offline: `uv run relay --help`, `RELAY_DISABLE_MODELS_FETCH=1 uv run relay models`, `RELAY_DISABLE_MODELS_FETCH=1 uv run relay config show`. Prefix CLI invocations with `TTY_COMPATIBLE=0` if you intend to assert on/parse their output.

The actual agent loop (`relay run -g "<goal>"`, `relay demo`, `relay doctor`, the `tui`) calls a live LLM and requires an API key — `OPENROUTER_API_KEY` (default provider for both roles) and/or `DEEPSEEK_API_KEY`. Set it as a Cloud secret or via a `.env` in the working directory (see `.env.example`). Without a key, `relay run` exits non-zero with a clear message (it does not hang). `RELAY_DISABLE_MODELS_FETCH=1` avoids the model-catalog network fetch.

### Lint
There is no linter configured in this repo (no ruff/flake8; `pyproject.toml` only declares `pytest` as a dev dependency). CI quality checks lockfile, build, CLI smoke, website version sync, and gitleaks — not ruff. "Checks" = the pytest suite above.
