"""Slash-command registry for the Relay TUI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

@dataclass(frozen=True)
class Command:
    """One slash command as a data record.

    ``name`` is the slash trigger (``"model"`` -> typed ``/model``); ``title`` /
    ``description`` are human text; ``category`` groups it in lists; ``run(app)``
    opens a dialog or performs the action (it takes only the app -- never a value
    parsed from the input); ``enabled(app)`` optionally hides the command in the
    current state (e.g. mid-run). Adding a command is adding a record to
    :data:`COMMANDS`.
    """

    name: str
    title: str
    description: str
    category: str
    run: Callable  # run(app) -> None
    enabled: Callable | None = None  # enabled(app) -> bool


def _run_active(app) -> bool:
    """Whether a run is in flight (used by ``enabled`` predicates)."""
    runner = getattr(app, "_runner", None)
    # ``EngineRunner`` records its terminal outcome before invoking the UI's
    # ``on_finished`` callback.  The worker thread can remain alive for a tiny
    # tail while that callback returns, but it cannot mutate the transcript or
    # execute more work once ``outcome`` is set.  Treat that settled tail as
    # inactive so a user who interrupts, stops, and immediately runs ``/clear``
    # does not hit a silent no-op race.
    return (
        runner is not None
        and getattr(runner, "is_running", False)
        and getattr(runner, "outcome", None) is None
    )


def _parse_inline_command(text: str) -> tuple[str, str] | None:
    """Parse ``/name arg...`` into ``(name, arg)``; ``None`` if not a slash command.

    Only the v0.0.28 inline-arg commands (``/queue`` / ``/redirect``) use this; every
    other slash command stays argument-free and runs via the popover."""
    if not text.startswith("/"):
        return None
    parts = text[1:].split(None, 1)
    if not parts:
        return None
    return parts[0], (parts[1].strip() if len(parts) > 1 else "")


def visible_commands(app) -> list[Command]:
    """Commands available in the app's current state (``enabled`` honored)."""
    return [c for c in COMMANDS if c.enabled is None or c.enabled(app)]


def filter_commands(app, query: str) -> list[Command]:
    """Visible commands whose name/title matches ``query`` (substring; empty = all)."""
    q = (query or "").strip().lower()
    out = []
    for command in visible_commands(app):
        if not q or q in command.name.lower() or q in command.title.lower():
            out.append(command)
    return out


# The registry -- one list; adding a command is adding a record. run(app) opens a
# dialog or does a clean action. Categories group the list in /help and the popover.
COMMANDS: list[Command] = [
    Command("help", "Help", "List all commands", "general",
            run=lambda app: app._cmd_help()),
    Command("model", "Model", "Pick the model for a role", "config",
            run=lambda app: app._cmd_model()),
    Command("provider", "Provider", "Set a role's provider, then its model", "config",
            run=lambda app: app._cmd_provider()),
    Command("key", "Key", "Add a provider API key (masked)", "config",
            run=lambda app: app._cmd_key()),
    Command("config", "Config", "Show the resolved config", "config",
            run=lambda app: app._cmd_config()),
    Command("doctor", "Doctor", "Preflight each role's provider/model", "ops",
            run=lambda app: app._cmd_doctor()),
    Command("runs", "Runs", "List recent runs", "ops",
            run=lambda app: app._cmd_runs()),
    Command("assume", "Assume", "Set the assumption level for this session", "ops",
            run=lambda app: app._cmd_assume()),
    Command("profile", "Profile", "Set assumption profile (surgeon/contractor/intern/chaos)", "ops",
            run=lambda app: app._cmd_profile()),
    Command("cwd", "Working dir", "Show / set the session working directory", "ops",
            run=lambda app: app._cmd_cwd(), enabled=lambda app: not _run_active(app)),
    Command("redirect", "Redirect", "Steer now: redirect the work (or /redirect <input>)", "ops",
            run=lambda app: app._open_inline_dialog("redirect")),
    Command("queue", "Queue", "Do this next: queue input (or /queue <input>)", "ops",
            run=lambda app: app._open_inline_dialog("queue")),
    Command("cost", "Cost", "Session + per-goal spend; toggle / reset the counter", "ops",
            run=lambda app: app._cmd_cost()),
    Command("why", "Why", "Harness flight recorder for the last/current run (zero tokens)", "ops",
            run=lambda app: app._cmd_why()),
    Command("route", "Route", "Show the spend-broker route contract / session pin", "ops",
            run=lambda app: app._cmd_route()),
    Command("memory", "Memory", "List / pin / forget durable shared findings", "ops",
            run=lambda app: app._cmd_memory()),
    Command("log", "Log", "Export a debug log (.md) to share when reporting an issue", "ops",
            run=lambda app: app._cmd_log()),
    Command("clear", "Clear", "Clear the stream + start a fresh session", "ops",
            run=lambda app: app._cmd_clear(), enabled=lambda app: not _run_active(app)),
]
