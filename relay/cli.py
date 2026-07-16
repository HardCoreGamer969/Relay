"""The Relay CLI.

``relay models`` shows the role → model mapping; ``relay demo`` runs the
brain → hands seam once; ``relay run`` drives the two-role planner/executor
loop against a goal (``--solo <role>`` falls back to the single-model loop).
Every command prints telemetry, now naturally split brain vs hands.
"""

from __future__ import annotations

import os
import subprocess
import time

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from relay.catalog import load_catalog
from relay.client import build_client
from relay.config import (
    ROLES,
    default_config,
    describe_resolution,
    load_models,
    resolve_assumption_level,
    resolve_bash_timeout,
    resolve_envelope_scope,
    resolve_envelope_warn,
    resolve_max_cost,
    resolve_max_total_steps,
)
from relay.context import resolve_context_window
from relay.envelope import CostEnvelope
from relay.providers import (
    DEFAULT_PROVIDER,
    DISCOVERY_LIST,
    known_providers,
    list_models,
    resolve_provider,
    validate_model,
)
from relay.secrets import remove_key, set_key
from relay.store import CONFIG_VERSION, load_config, save_config
from relay.conversation import DEFAULT_MAX_ROUNDS, plan_conversationally
from relay.loop import (
    STATUS_COMPLETED,
    STATUS_MAX_COST as SOLO_STATUS_MAX_COST,
    STATUS_MAX_STEPS,
    STATUS_PARSE_FAILURE_ABORT,
    StepResult,
    run_task,
)
from relay.models import call_model
from relay.orchestrator import (
    STATUS_ABORTED_BY_BRAIN,
    STATUS_DECLINED,
    STATUS_ESCALATION_LIMIT,
    STATUS_MAX_COST,
    STATUS_PLANNING_FAILED,
    STATUS_REPEATED_STEP,
    STATUS_UNRESOLVED_ESCALATION,
    Event,
    PlannedTaskResult,
    friendly_terminal_message,
    run_planned,
)
from relay.runlog import append_record, build_record, default_log_path, load_records
from relay.telemetry import Ledger
from relay.transcript import Transcript

app = typer.Typer(
    help="Relay - a planner/executor coding agent on a model-agnostic OpenRouter seam.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


@app.command()
def models() -> None:
    """Print each role's resolved (provider, model) -- multi-provider aware (v0.0.32)."""
    cfg = load_models()
    table = Table(title="Relay: roles -> models")
    table.add_column("Role", style="bold cyan")
    table.add_column("Provider", style="green")
    table.add_column("Model", style="green")
    table.add_row("brain (planner)", cfg.brain_provider, cfg.brain)
    table.add_row("hands (executor)", cfg.hands_provider, cfg.hands)
    console.print(table)


@app.command()
def demo(
    goal: str = typer.Option(
        ..., "--goal", "-g", help="The goal to relay through the brain -> hands seam."
    ),
) -> None:
    """Run the brain -> hands seam once for a goal, then print telemetry."""
    cfg = load_models()
    ledger = Ledger()

    brain_system = (
        "You are the planner. Output EXACTLY ONE concrete next step toward the "
        "goal, as a single sentence. No preamble, no numbering, no explanation."
    )
    hands_system = (
        "You are the executor. Given a single step, say in 1-2 sentences how you "
        "would carry it out. Do not expand scope or add any further steps."
    )

    try:
        brain = call_model(
            "brain",
            [
                {"role": "system", "content": brain_system},
                {"role": "user", "content": f"Goal: {goal}"},
            ],
            models=cfg,
            ledger=ledger,
        )
        hands = call_model(
            "hands",
            [
                {"role": "system", "content": hands_system},
                {"role": "user", "content": f"Step: {brain.text}"},
            ],
            models=cfg,
            ledger=ledger,
        )
    except Exception as exc:  # noqa: BLE001 — surface any failure as a friendly message
        console.print(
            Panel(
                f"[red]Model call failed:[/red] {exc}\n\n"
                "Relay reaches every model through OpenRouter. Make sure "
                "[bold]OPENROUTER_API_KEY[/bold] is set - copy "
                "[bold].env.example[/bold] to [bold].env[/bold] and add your key.",
                title="Relay demo: error",
                border_style="red",
            )
        )
        raise typer.Exit(code=1)

    console.print(
        Panel(brain.text, title=f"brain (planner) -> {cfg.brain}", border_style="cyan")
    )
    console.print(
        Panel(hands.text, title=f"hands (executor) -> {cfg.hands}", border_style="green")
    )
    _print_telemetry(ledger)


@app.command()
def run(
    goal: str = typer.Option(
        ..., "--goal", "-g", help="The goal for the agent to accomplish."
    ),
    root: str = typer.Option(
        ".", "--root", help="Project root the agent's tools are confined to."
    ),
    auto_approve: bool = typer.Option(
        False,
        "--auto-approve",
        "-y",
        help="Auto-approve CONFIRM-category commands (BLOCKED stays refused).",
    ),
    solo: str = typer.Option(
        "",
        "--solo",
        help="Run the single-model loop with this role (e.g. 'hands') instead of "
        "the two-role planner/executor. For comparison/debugging.",
    ),
    confirm_plan: bool = typer.Option(
        False,
        "--confirm-plan",
        help="Pause for approval after the brain produces the plan, before execution.",
    ),
    max_steps: int = typer.Option(
        20, "--max-steps", help="Max model turns (solo mode only)."
    ),
    max_total_steps: int | None = typer.Option(
        None, "--max-total-steps",
        help="Global executor-step ceiling for a planned run (a safety net). "
        "Default 50; pass 0 to disable (unbounded). Overrides RELAY_MAX_TOTAL_STEPS "
        "and config for this run.",
    ),
    max_cost: float | None = typer.Option(
        None, "--max-cost",
        help="Hard cost ceiling (dollars). The run halts at a step/turn boundary "
        "when chargeable spend crosses this value. Default unbounded; pass 0 to "
        "disable. Overrides RELAY_MAX_COST and config for this run.",
    ),
    envelope_scope: str = typer.Option(
        "",
        "--envelope-scope",
        help="Cost-envelope scope (cost ceiling only; does not change step ceilings): "
        "'all' counts planning+execution (default), 'execution' excludes planning spend. "
        "Overrides RELAY_ENVELOPE_SCOPE / config.",
    ),
    envelope_warn: str = typer.Option(
        "",
        "--envelope-warn",
        help="Comma-separated warn fractions of each active ceiling "
        "(default 0.5,0.8,0.9,0.99). Overrides RELAY_ENVELOPE_WARN / config.",
    ),
    no_supervise: bool = typer.Option(
        False, "--no-supervise",
        help="Disable brain supervision (no step-boundary review calls).",
    ),
    assume: str = typer.Option(
        "", "--assume",
        help="Assumption dial: 1 (assume freely) .. 5 (follow the letter) or 'auto'. "
        "Overrides RELAY_ASSUMPTION_LEVEL for this run.",
    ),
    show_transcript: bool = typer.Option(
        False, "--show-transcript",
        help="After the run, print the (compacted) continuous conversation thread "
        "-- the plain-CLI preview of scroll-back (the scrollable view is the TUI).",
    ),
    plan_only: bool = typer.Option(
        False, "--plan-only",
        help="Run the planning conversation, print the committed plan, and exit "
        "WITHOUT executing. Useful for reviewing what the brain would do before "
        "spending money on execution. No files are written.",
    ),
    no_log: bool = typer.Option(
        False, "--no-log", help="Skip persisting this run to .relay/runs.jsonl."
    ),
) -> None:
    """Run the agent against a goal.

    Default: the two-role brain (planner) + hands (executor) architecture. Planning
    is a conversation -- the brain assesses scope, proposes (or asks, per the
    assumption dial), you react in plain language, and only on commit does it hand
    off to the autonomous loop. Use --solo <role> for the single-model loop. Each
    run is persisted to <root>/.relay/runs.jsonl (see `relay runs`) unless --no-log.
    """
    cfg = load_models()
    ledger = Ledger()
    approver = None if auto_approve else _interactive_approver
    dial = resolve_assumption_level(override=assume or None)
    ceiling = resolve_max_total_steps(override=max_total_steps)  # 50 by default; flag/env/config can raise
    cost_ceiling = resolve_max_cost(override=max_cost)
    scope = resolve_envelope_scope(override=envelope_scope or None)
    warn_thresholds = resolve_envelope_warn(override=envelope_warn or None)
    bash_timeout = resolve_bash_timeout()
    _warn_if_dirty_git(root)

    mode = "solo" if solo else "planned"
    # Solo uses --max-steps as the step dimension; planned uses the global ceiling.
    step_ceiling = max_steps if solo else ceiling
    envelope = CostEnvelope(
        max_cost=cost_ceiling,
        max_steps=step_ceiling,
        scope=scope,
        warn_thresholds=warn_thresholds,
    )
    start = time.perf_counter()
    if solo:
        result = _run_solo(
            goal, root, solo, max_steps, cfg, ledger, auto_approve, approver,
            bash_timeout_s=bash_timeout, envelope=envelope,
        )
    else:
        result = _run_planned(
            goal, root, cfg, ledger, auto_approve, approver, confirm_plan,
            supervise=not no_supervise, dial=dial, show_transcript=show_transcript,
            max_total_steps=ceiling, max_cost=cost_ceiling, bash_timeout_s=bash_timeout,
            plan_only=plan_only, envelope=envelope,
        )
    wall_time_s = time.perf_counter() - start

    _print_telemetry(ledger)
    _print_receipt(envelope, ledger, status=getattr(result, "status", ""))
    # A3: persist shared findings/directives for the next run in this project.
    try:
        from relay.durable_memory import capture_bus_shared
        mem = getattr(result, "memory", None)
        if mem is not None:
            capture_bus_shared(mem, root)
    except OSError:
        pass
    if not no_log:
        _save_run(goal=goal, mode=mode, result=result, ledger=ledger, cfg=cfg,
                  wall_time_s=wall_time_s, root=root)


def _run_solo(goal, root, role, max_steps, cfg, ledger, auto_approve, approver, *,
               bash_timeout_s=120.0, envelope: CostEnvelope | None = None):
    """The v0.02 single-model loop, kept for comparison/debugging."""
    bash_policy = "auto-approve" if auto_approve else "interactive approval"
    env = envelope or CostEnvelope(max_steps=max_steps)
    console.print(
        Panel(
            f"[bold]{goal}[/bold]\nroot={root}  solo role={role}  max-steps={max_steps}  "
            f"bash policy={bash_policy}\n{env.preflight_text()}",
            title="Relay run (solo)",
            border_style="cyan",
        )
    )

    def _warn(payload: dict) -> None:
        console.print(f"[yellow]{payload.get('message', 'envelope warn')}[/yellow]")

    try:
        result = run_task(
            goal, root, role=role, max_steps=max_steps, models=cfg, ledger=ledger,
            on_step=_print_step, approver=approver, auto_approve=auto_approve,
            bash_timeout_s=bash_timeout_s, envelope=env, on_envelope_warn=_warn,
        )
    except Exception as exc:  # noqa: BLE001 — surface any failure as a friendly message
        _print_run_error(exc)
        raise typer.Exit(code=1)

    if result.status == STATUS_COMPLETED:
        console.print(f"\n[bold green]done:[/bold green] {result.done_summary}")
    elif result.status == STATUS_MAX_STEPS:
        console.print(
            f"\n[yellow]stopped: hit max-steps ({max_steps}) without <done>[/yellow]"
        )
    elif result.status in (SOLO_STATUS_MAX_COST, STATUS_MAX_COST):
        msg = friendly_terminal_message(STATUS_MAX_COST, max_total_steps=max_steps)
        console.print(f"\n[yellow]{msg or 'stopped: cost ceiling reached'}[/yellow]")
    elif result.status == STATUS_PARSE_FAILURE_ABORT:
        console.print(
            "\n[bold red]aborted: too many consecutive parse failures[/bold red]"
        )
    else:
        console.print(f"\n[yellow]stopped: {result.status}[/yellow]")
    return result


def _run_planned(goal, root, cfg, ledger, auto_approve, approver, confirm_plan, *,
                 supervise=True, dial="auto", show_transcript=False, max_total_steps=None,
                 max_cost=None, bash_timeout_s=120.0, plan_only=False,
                 envelope: CostEnvelope | None = None):
    """Conversational planning -> commit -> the two-role autonomous loop.

    Both phases share ONE transcript, so a mid-run escalation appears as the next
    turn of the same conversation rather than a separate popup.
    """
    bash_policy = "auto-approve" if auto_approve else "interactive approval"
    env = envelope or CostEnvelope(max_cost=max_cost, max_steps=max_total_steps)
    console.print(
        Panel(
            f"[bold]{goal}[/bold]\nroot={root}\nbrain={cfg.brain}\nhands={cfg.hands}\n"
            f"bash policy={bash_policy}  supervision={'on' if supervise else 'off'}  "
            f"assume={dial}\n{env.preflight_text()}",
            title="Relay run (brain + hands)",
            border_style="cyan",
        )
    )

    transcript = Transcript()  # the one continuous thread across planning + execution

    def _on_conv_event(kind, message, payload):
        _print_conv_event(kind, message, payload)
        if kind == "envelope_warn":
            console.print(f"[yellow]{message}[/yellow]")

    # 1. Plan as a conversation (--confirm-plan = the degenerate 1-round case).
    try:
        conversation = plan_conversationally(
            goal, root, models=cfg, ledger=ledger, client=None,
            assumption_level=dial, user_turn=_interactive_user_turn,
            max_rounds=1 if confirm_plan else DEFAULT_MAX_ROUNDS,
            on_event=_on_conv_event, transcript=transcript, envelope=env,
        )
    except Exception as exc:  # noqa: BLE001 — surface any failure as a friendly message
        _print_run_error(exc)
        raise typer.Exit(code=1)

    if conversation.stop_reason == "max_cost":
        console.print("\n[yellow]stopped during planning: cost ceiling reached[/yellow]")
        return PlannedTaskResult(
            goal=goal, plan=conversation.plan, status=STATUS_MAX_COST,
            ledger=ledger, transcript=transcript, envelope=env, max_cost=env.max_cost,
        )

    if not conversation.committed or conversation.plan is None or not conversation.plan.steps:
        console.print("\n[yellow]plan not committed; nothing executed[/yellow]")
        return PlannedTaskResult(goal=goal, plan=conversation.plan, status=STATUS_DECLINED,
                                 ledger=ledger, transcript=transcript, envelope=env)

    # Post-plan snapshot (spent so far / remaining) before hands execute.
    if env.max_cost is not None or (ledger.total_cost() or 0) > 0:
        console.print(f"[dim]{env.post_plan_snapshot(ledger)}[/dim]")
    if plan_only:
        steps = conversation.plan.steps
        body = "\n".join(f"{i}. {s.instruction}" for i, s in enumerate(steps, 1))
        console.print(Panel(body, title=f"Planned plan ({len(steps)} step(s), NOT executed)",
                            border_style="magenta"))
        console.print("[dim](--plan-only: no files were written, no executor calls made)[/dim]")
        return PlannedTaskResult(goal=goal, plan=conversation.plan, status=STATUS_DECLINED,
                                 ledger=ledger, transcript=transcript, envelope=env)

    # 2. Execute the committed plan on the SAME thread (the dial keeps biasing
    #    answer_or_escalate; escalations continue the conversation above).
    def _on_exec_event(event: Event) -> None:
        _print_event(event)
        if event.kind == "envelope_warn":
            console.print(f"[yellow]{event.message}[/yellow]")

    try:
        from relay.catalog import get_catalog
        catalog = get_catalog()
        result = run_planned(
            goal, root, models=cfg, ledger=ledger, client=None,
            approver=approver, auto_approve=auto_approve,
            supervise=supervise, user_decision=_interactive_user_decision,
            assumption_level=dial, committed_plan=conversation.plan,
            on_event=_on_exec_event, transcript=transcript,
            max_total_steps=max_total_steps, max_cost=max_cost,
            catalog=catalog,
            bash_timeout_s=bash_timeout_s,
            envelope=env,
        )
    except Exception as exc:  # noqa: BLE001 — surface any failure as a friendly message
        _print_run_error(exc)
        raise typer.Exit(code=1)
    _print_planned_status(result)
    if show_transcript:
        _print_transcript(result.transcript_compacted or result.transcript or transcript)
    return result


def _interactive_user_turn(prompt: str) -> str:
    """Conversation seam: show the brain's question or proposed plan, read a reply."""
    console.print(Panel(prompt, title="Planning", border_style="cyan"))
    return typer.prompt("You")


def _print_conv_event(kind: str, message: str, payload: dict) -> None:
    """Render planning-conversation events (ASCII-safe; the prompts themselves
    are shown by the interactive user_turn panel)."""
    if kind == "scope_assessed":
        console.print(
            f"[magenta]scope:[/magenta] {payload.get('scope')} -> posture "
            f"{payload.get('posture')}  ([dim]{payload.get('reason', '')}[/dim])"
        )
    elif kind == "plan_proposed":
        console.print("[magenta]brain proposed a plan[/magenta]")
    elif kind == "plan_revised":
        console.print("[magenta]brain revised the plan from your reaction[/magenta]")
    elif kind == "committed":
        console.print("[green]plan committed -- handing to the autonomous loop[/green]")
    elif kind == "not_committed":
        console.print(f"[yellow]{message}[/yellow]")


def _interactive_user_decision(question: str) -> str:
    """Escalation seam: the brain's product question as the NEXT TURN of the same
    conversation -- not a differently-styled popup.

    The brain phrased this as a continuation (it was handed the conversation so
    far), so it is rendered in cyan to match the conversational tone rather than as
    a separate magenta "decision required" popup. Product decisions are NEVER
    auto-answered -- not even with --auto-approve (that only covers CONFIRM bash).
    """
    console.print(Panel(question, title="Conversation (the agent is continuing)", border_style="cyan"))
    return typer.prompt("You")


def _print_transcript(transcript) -> None:
    """Print the (compacted) continuous conversation thread: the plain-CLI preview
    of scroll-back. ASCII-safe; long turn text is folded, not ellipsis-truncated.
    The scrollable interactive view is the TUI milestone."""
    turns = getattr(transcript, "turns", None) or []
    if not turns:
        console.print("[dim](no conversation thread to show)[/dim]")
        return
    table = Table(title="Conversation thread (compacted preview)")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Who", style="bold")
    table.add_column("Phase", style="cyan")
    # Fold long turn text rather than ellipsis-truncate (the legacy-Windows lesson).
    table.add_column("Said", overflow="fold")
    for turn in turns:
        who = "you" if turn.speaker == "user" else "brain"
        table.add_row(str(turn.created_at), who, turn.phase, " ".join((turn.text or "").split()))
    console.print(table)


def _save_run(*, goal, mode, result, ledger, cfg, wall_time_s, root) -> None:
    """Persist the finished run. Logging must NEVER crash a run -- warn and go on."""
    try:
        record = build_record(
            goal=goal, mode=mode, result=result, ledger=ledger, models=cfg,
            wall_time_s=wall_time_s,
        )
        path = default_log_path(root)
        append_record(record, path)
        console.print(f"[dim]saved run {record.run_id} -> {path}[/dim]")
    except Exception as exc:  # noqa: BLE001 — the run result matters more than the log
        console.print(f"[yellow]note:[/yellow] could not save run log: {exc}")


@app.command()
def runs(
    limit: int = typer.Option(
        10, "--limit", help="How many recent runs to show (most recent first)."
    ),
    root: str = typer.Option(
        ".", "--root", help="Project root whose .relay/runs.jsonl to read."
    ),
    explain: str = typer.Option(
        "",
        "--explain",
        help="Print the harness flight recorder (/why) for a run_id prefix "
        "(deterministic, zero model tokens). Example: relay runs --explain 20260716",
    ),
) -> None:
    """Show recently recorded runs from <root>/.relay/runs.jsonl."""
    path = default_log_path(root)
    records = load_records(path)
    if not records:
        console.print(f"[yellow]no runs recorded yet[/yellow] (looked in {path})")
        return
    if explain:
        needle = explain.strip()
        match = next((r for r in reversed(records) if r.run_id.startswith(needle)), None)
        if match is None:
            console.print(f"[yellow]no run matching id prefix {needle!r}[/yellow]")
            raise typer.Exit(code=1)
        harness = match.harness if isinstance(getattr(match, "harness", None), dict) else None
        if not harness:
            console.print(
                f"[yellow]run {match.run_id} has no harness snapshot "
                f"(older runs before A2)[/yellow]"
            )
            raise typer.Exit(code=1)
        from relay.explain import HarnessReport

        report = HarnessReport(**{k: harness.get(k, getattr(HarnessReport(), k)) for k in HarnessReport().__dict__})
        # Simpler: render from dict keys we care about
        console.print(_harness_text_from_dict(harness, run_id=match.run_id))
        return
    console.print(_runs_table(records, limit))


def _harness_text_from_dict(harness: dict, *, run_id: str = "") -> str:
    from relay.explain import HarnessReport

    fields = HarnessReport.__dataclass_fields__
    kwargs = {k: harness[k] for k in fields if k in harness}
    report = HarnessReport(**kwargs)
    header = f"run_id: {run_id}\n" if run_id else ""
    return header + report.to_text()


def _fmt_ts(iso: str) -> str:
    """Shorten an ISO timestamp to 'YYYY-MM-DD HH:MM:SS' for display."""
    text = (iso or "")[:19]
    return text.replace("T", " ") if text else "-"


def _runs_table(records, limit: int) -> Table:
    """Build a rich Table of the most-recent ``limit`` runs (newest first)."""
    recent = list(reversed(records))[: max(limit, 0)]
    table = Table(title=f"Relay runs (showing {len(recent)} of {len(records)})")
    table.add_column("When", style="dim")
    table.add_column("Mode")
    table.add_column("Models", overflow="fold")
    # Fold long values rather than ellipsis-truncate: Rich's ellipsis mojibakes
    # on the legacy Windows console (the v0.04 telemetry lesson).
    table.add_column("Status", overflow="fold")
    table.add_column("Cost (USD)", justify="right")
    table.add_column("Tokens", justify="right")
    table.add_column("Steps", justify="right")
    for rec in recent:
        roles = rec.roles if isinstance(rec.roles, dict) else {}
        models_text = "\n".join(f"{role}: {model}" for role, model in roles.items()) or "-"
        totals = rec.totals if isinstance(rec.totals, dict) else {}
        cost = totals.get("cost_usd")
        cost_text = "-" if cost is None else f"${cost:.6f}"
        style = "green" if rec.status == "completed" else "yellow"
        table.add_row(
            _fmt_ts(rec.timestamp),
            rec.mode,
            models_text,
            f"[{style}]{rec.status}[/{style}]",
            cost_text,
            str(totals.get("tokens", "")),
            str(rec.steps),
        )
    return table


@app.command()
def memory(
    action: str = typer.Argument(
        "list",
        help="list | pin <id> | forget <id>",
    ),
    entry_id: str = typer.Argument("", help="Entry id for pin/forget."),
    root: str = typer.Option(".", "--root", help="Project root for .relay/memory.json."),
) -> None:
    """Manage durable shared findings/directives (A3). Never touches brain-private memory."""
    from relay.durable_memory import forget_entry, list_entries, pin_entry

    act = (action or "list").strip().lower()
    if act == "list":
        entries = list_entries(root)
        if not entries:
            console.print("[dim](no durable shared memory yet)[/dim]")
            return
        table = Table(title="Durable shared memory")
        table.add_column("Id")
        table.add_column("Kind")
        table.add_column("Summary", overflow="fold")
        table.add_column("Tags")
        for e in entries:
            table.add_row(e.id, e.kind, e.summary, ",".join(e.tags) or "-")
        console.print(table)
        return
    if act == "pin":
        if not entry_id:
            console.print("[red]pin requires an entry id[/red]")
            raise typer.Exit(1)
        ok = pin_entry(root, entry_id)
        console.print("[green]pinned[/green]" if ok else f"[yellow]not found: {entry_id}[/yellow]")
        raise typer.Exit(0 if ok else 1)
    if act == "forget":
        if not entry_id:
            console.print("[red]forget requires an entry id[/red]")
            raise typer.Exit(1)
        ok = forget_entry(root, entry_id)
        console.print("[green]forgot[/green]" if ok else f"[yellow]not found: {entry_id}[/yellow]")
        raise typer.Exit(0 if ok else 1)
    console.print(f"[red]unknown action {action!r}; use list|pin|forget[/red]")
    raise typer.Exit(1)


@app.command()
def doctor(
    slugs: list[str] = typer.Argument(
        None,
        help="Optional model slugs to probe ad-hoc (assumed on the default "
        "provider); default checks the configured roles against their providers.",
    ),
) -> None:
    """Preflight: check each role's (provider, model) resolves on its provider's API.

    Each check is a minimal (max_tokens=1) live call against the role's provider
    (OpenRouter, DeepSeek, ...). It also reports the resolved context window + its
    source per role (catalog vs probe vs default) and the model-catalog status,
    then exits non-zero if any check failed -- usable as a scripted preflight that
    catches a retired-slug 404 (or a mis-wired provider) before a run spends money.
    """
    cfg = load_models()  # loads .env so the provider keys are visible
    checks = _doctor_checks(cfg, slugs)

    # Provider-aware key precheck: every provider in play needs a key --
    # either in the env var or in auth.json (``resolve_key`` is the single
    # source of truth -- v0.0.32: env-var-only check used to false-positive
    # on a user who set their key via ``relay config set-key``). Checked
    # BEFORE building any client (so a missing key never builds/charges).
    missing = _missing_provider_keys(checks)
    if missing:
        names = missing[0] if len(missing) == 1 else ", ".join(missing)
        verb = "is" if len(missing) == 1 else "are"
        console.print(
            f"[red]{names} {verb} not set[/red] - cannot probe models. "
            "Copy .env.example to .env, or set it with `relay config set-key <provider>`."
        )
        raise typer.Exit(code=1)

    clients = _build_provider_clients(checks)
    rows, all_ok = _run_doctor(checks, clients)
    _print_doctor_table(rows)

    # Surface the catalog source/status so a silent fallback (stale/bundled) shows.
    catalog = _safe_load_catalog()
    if catalog is not None:
        console.print(f"model catalog: {catalog.status} (source: {catalog.source})")

    # Per-role context window + its provenance (catalog vs probe vs default).
    for label, provider, model in checks:
        window, source = resolve_context_window(
            model, provider=provider, client=clients.get(provider), catalog=catalog
        )
        console.print(f"{label} context window: {window} tokens (source: {source})")
        if source == "default":
            console.print(
                "[yellow]note:[/yellow] guessing the window; declare it via RELAY_BRAIN_CONTEXT."
            )

    raise typer.Exit(code=0 if all_ok else 1)


def _doctor_checks(cfg, slugs) -> list[tuple[str, str, str]]:
    """The ``(label, provider, model)`` checks: ad-hoc slugs or the configured roles."""
    if slugs:
        return [("arg", DEFAULT_PROVIDER, s) for s in slugs]
    return [("brain", cfg.brain_provider, cfg.brain), ("hands", cfg.hands_provider, cfg.hands)]


def _missing_provider_keys(checks) -> list[str]:
    """Key env-vars (deduped, in order) that are needed by some provider but unset.

    v0.0.32: check both the env var AND the auth.json store -- ``build_client``
    accepts both (env > auth.json), so a key saved via ``relay config set-key``
    is just as usable as one in the env. Checking only ``os.environ`` here
    would report a false "not set" for a user who set their key in-app,
    and the doctor would hard-exit before even probing. ``resolve_key`` is
    the single source of truth for "is this provider's key present?".
    """
    from relay.secrets import resolve_key
    missing: list[str] = []
    seen: set[str] = set()
    for _, provider, _ in checks:
        if provider in seen:
            continue
        seen.add(provider)
        try:
            profile = resolve_provider(provider)
        except ValueError:
            continue  # unknown provider surfaces as a failed probe, not a key error
        if resolve_key(profile.id, profile.key_env) is None and profile.key_env not in missing:
            missing.append(profile.key_env)
    return missing


def _build_provider_clients(checks) -> dict:
    """Build one client per distinct provider; a build failure is a failed probe."""
    clients: dict = {}
    for _, provider, _ in checks:
        if provider in clients:
            continue
        try:
            clients[provider] = build_client(provider)
        except Exception as exc:  # noqa: BLE001 — surface as a failed probe, not a traceback
            console.print(f"[red]could not build the {provider} client:[/red] {exc}")
    return clients


def _safe_load_catalog():
    """Load the catalog for the status line; never let it crash doctor."""
    try:
        return load_catalog()
    except Exception:  # noqa: BLE001 — catalog status is informational only
        return None


def _probe_model(client, slug: str, provider: str = DEFAULT_PROVIDER) -> tuple[bool, str]:
    """Minimal (max_tokens=1) call to check a slug resolves. Never raises.

    Only the OpenRouter path asks for usage cost (its include flag); other
    providers get a plain probe.
    """
    try:
        extra_body = {"usage": {"include": True}} if provider == DEFAULT_PROVIDER else {}
        client.chat.completions.create(
            model=slug,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
            extra_body=extra_body,
        )
        return True, "resolved"
    except Exception as exc:  # noqa: BLE001 — classify any failure as the note
        text = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
        return False, text[:120]


def _run_doctor(checks, clients) -> tuple[list[dict], bool]:
    """Probe each ``(label, provider, slug)`` against its provider's client."""
    rows: list[dict] = []
    all_ok = True
    for label, provider, slug in checks:
        client = clients.get(provider)
        if client is None:
            ok, note = False, f"no client for provider {provider!r}"
        else:
            ok, note = _probe_model(client, slug, provider)
        rows.append(
            {"role": label, "provider": provider, "model": slug,
             "status": "OK" if ok else "FAILED", "note": note}
        )
        all_ok = all_ok and ok
    return rows, all_ok


def _print_doctor_table(rows) -> None:
    table = Table(title="Relay doctor: provider/model preflight")
    table.add_column("Role", style="bold")
    table.add_column("Provider")
    table.add_column("Model", overflow="fold")
    table.add_column("Status")
    table.add_column("Note", overflow="fold")
    for row in rows:
        style = "green" if row["status"] == "OK" else "bold red"
        table.add_row(
            row["role"], row.get("provider", ""), row["model"],
            f"[{style}]{row['status']}[/{style}]", row["note"],
        )
    console.print(table)


# --- `relay config`: manage providers, models, and keys ---------------------

config_app = typer.Typer(
    help="Manage providers, per-role models, and provider keys (persistent global config).",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(config_app, name="config")


def _validate_role_model(provider: str, model: str) -> tuple[bool, str]:
    """Validate a (provider, model) before saving it (shared with the TUI).

    Manual providers (OpenRouter) get a live preflight probe; list providers
    (DeepSeek) must appear in the live ``/models`` list. Never raises.
    """
    return validate_model(provider, model)


@config_app.command("show")
def config_show() -> None:
    """Show the resolved config (provider/model/thinking + source per role) and
    whether a key is present per provider. NEVER prints a key."""
    resolution = describe_resolution()
    table = Table(title="Relay config (resolved: env > config.json > default)")
    table.add_column("Role", style="bold")
    table.add_column("Provider", overflow="fold")
    table.add_column("Model", overflow="fold")
    table.add_column("Thinking")
    table.add_column("Source", overflow="fold")
    for role in ROLES:
        fields = resolution["roles"][role]
        provider, p_src = fields["provider"]
        model, m_src = fields["model"]
        thinking, t_src = fields["thinking"]
        table.add_row(
            role, provider, model, "on" if thinking else "off",
            f"provider={p_src} model={m_src} thinking={t_src}",
        )
    console.print(table)

    keys = Table(title="Provider keys (env var or stored auth.json)")
    keys.add_column("Provider", style="bold")
    keys.add_column("Key")
    for pid in known_providers():
        present = resolution["providers"][pid]["key_present"]
        status = "[green]present[/green]" if present else "[yellow]absent[/yellow]"
        keys.add_row(pid, status)  # presence only -- the key value is NEVER shown
    console.print(keys)


@config_app.command("set-role")
def config_set_role(
    role: str = typer.Argument(..., help="Which role to configure: brain or hands."),
    provider: str = typer.Option(..., "--provider", "-p", help="Provider id (openrouter / deepseek)."),
    model: str = typer.Option(..., "--model", "-m", help="Model id/slug for this role."),
    thinking: bool = typer.Option(False, "--thinking/--no-thinking", help="Enable thinking mode for this role."),
) -> None:
    """Set a role's provider + model (+ thinking) in config.json.

    The (provider, model) is validated LIVE before saving -- a typo'd slug is
    rejected here, not at first run.
    """
    if role not in ROLES:
        console.print(f"[red]unknown role {role!r}[/red] -- valid roles: {', '.join(ROLES)}")
        raise typer.Exit(code=1)
    try:
        resolve_provider(provider)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    ok, note = _validate_role_model(provider, model)
    if not ok:
        console.print(f"[red]rejected:[/red] {note}")
        console.print("[dim]nothing was saved.[/dim]")
        raise typer.Exit(code=1)

    config = load_config() or default_config()
    config.setdefault("version", CONFIG_VERSION)
    config.setdefault("roles", {})[role] = {
        "provider": provider, "model": model, "thinking": bool(thinking),
    }
    path = save_config(config)
    console.print(
        f"[green]saved[/green] {role}: {provider} / {model} "
        f"(thinking {'on' if thinking else 'off'})  [dim]-> {path}[/dim]  ({note})"
    )


@config_app.command("set-key")
def config_set_key(
    provider: str = typer.Argument(..., help="Provider to store a key for (openrouter / deepseek)."),
) -> None:
    """Store a provider API key in auth.json (0o600). The key is read WITHOUT echo
    and is never printed or shell-historied."""
    try:
        resolve_provider(provider)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)
    key = typer.prompt(f"API key for {provider}", hide_input=True)  # no echo
    if not key.strip():
        console.print("[yellow]no key entered; nothing saved.[/yellow]")
        raise typer.Exit(code=1)
    path = set_key(provider, key.strip())
    console.print(f"[green]stored a key for {provider}[/green] [dim]-> {path} (0o600)[/dim]")


@config_app.command("remove-key")
def config_remove_key(
    provider: str = typer.Argument(..., help="Provider whose stored key to remove."),
) -> None:
    """Remove a provider's stored key from auth.json (no-op if none stored)."""
    remove_key(provider)
    console.print(f"[green]removed any stored key for {provider}[/green]")


@config_app.command("list-models")
def config_list_models(
    provider: str = typer.Argument(..., help="Provider to list models for (openrouter / deepseek)."),
) -> None:
    """List a provider's models. Direct providers (DeepSeek) list live from
    ``/models``; aggregators (OpenRouter) are manual slug entry."""
    try:
        profile = resolve_provider(provider)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    if profile.discovery != DISCOVERY_LIST:
        console.print(
            f"[yellow]{provider} is manual slug entry[/yellow] -- any {provider} model slug "
            "works; there is no live list to enumerate."
        )
        return

    try:
        ids = list_models(provider)
    except Exception as exc:  # noqa: BLE001 -- missing key / network: a clear message
        console.print(f"[red]could not list {provider} models:[/red] {str(exc).splitlines()[0]}")
        raise typer.Exit(code=1)
    if not ids:
        console.print(f"[yellow]no models returned for {provider}.[/yellow]")
        return

    catalog = _safe_load_catalog()
    table = Table(title=f"{provider}: live models (/models)")
    table.add_column("Model id", style="green", overflow="fold")
    table.add_column("Context", justify="right")
    table.add_column("In $/1M", justify="right")
    table.add_column("Out $/1M", justify="right")
    for mid in ids:
        ctx = catalog.context_limit(provider, mid) if catalog is not None else None
        cost = catalog.cost(provider, mid) if catalog is not None else None
        in_p = "-" if cost is None or cost.input is None else f"{cost.input:g}"
        out_p = "-" if cost is None or cost.output is None else f"{cost.output:g}"
        table.add_row(mid, "-" if ctx is None else str(ctx), in_p, out_p)
    console.print(table)


@app.command()
def tui(
    root: str = typer.Option(
        ".", "--root", help="Project root the agent's tools are confined to."
    ),
    auto_approve: bool = typer.Option(
        False,
        "--auto-approve",
        "-y",
        help="Auto-approve CONFIRM-category commands (BLOCKED stays refused).",
    ),
    assume: str = typer.Option(
        "", "--assume",
        help="Assumption dial: 1 (assume freely) .. 5 (follow the letter) or 'auto'. "
        "Overrides RELAY_ASSUMPTION_LEVEL for this run.",
    ),
) -> None:
    """Launch the Relay TUI: an interactive chat over the brain + hands loop.

    Opens straight to an empty chat on the env-configured models (the model
    indicator shows the brain/hands pairing before the first message). The
    plain CLI (`relay run`, `--solo`, `runs`, `doctor`, ...) is unchanged --
    the TUI is additive, for interactive use; the plain path stays for
    scripting/headless.
    """
    # Lazy import: textual is only loaded when the TUI is actually launched.
    from relay.tui import RelayTuiApp

    cfg = load_models()
    dial = resolve_assumption_level(override=assume or None)
    _warn_if_dirty_git(root)
    # Load the catalog so run_planned can resolve each actor's real context window
    # (without it the window always falls to 8192 and memory budgets are stunted).
    catalog = _safe_load_catalog()
    RelayTuiApp(
        root=root, models=cfg, assumption_level=dial, auto_approve=auto_approve,
        catalog=catalog,
    ).run()


def _confirm_plan_gate(plan) -> bool:
    """Ask the user to approve the plan before execution (the plan is already shown)."""
    return typer.confirm("Proceed with this plan?", default=True)


def _print_event(event: Event) -> None:
    """Render one streamed orchestration event (ASCII-safe)."""
    kind = event.kind
    if kind == "plan_created":
        steps = event.payload.get("steps", [])
        body = "\n".join(f"{i}. {s}" for i, s in enumerate(steps)) or "(no steps)"
        console.print(Panel(body, title="Plan (brain)", border_style="magenta"))
    elif kind in ("replanned", "plan_revised"):
        steps = event.payload.get("steps", [])
        body = "\n".join(f"- {s}" for s in steps) or "(no steps)"
        title = "Revised plan (brain, after learning)" if kind == "plan_revised" else "Revised plan (brain)"
        console.print(Panel(body, title=title, border_style="magenta"))
    elif kind == "brain_action":
        console.print(f"  [magenta]brain[/magenta] {event.message}")
    elif kind == "step_start":
        console.print(
            f"\n[bold cyan]> step {event.payload.get('index')}:[/bold cyan] "
            f"{event.payload.get('instruction')}"
        )
    elif kind == "exec_action":
        observation = " ".join((event.payload.get("observation") or "").split())
        console.print(f"  [cyan].[/cyan] {event.message}")
        if observation:
            if observation.startswith("BLOCKED by policy") or observation.startswith("DENIED"):
                console.print(f"    [bold red]{observation[:200]}[/bold red]")
            else:
                console.print(f"    [dim]{observation[:200]}[/dim]")
    elif kind == "exec_parse_failure":
        console.print(f"  [red]! parse failure[/red] {event.payload.get('snippet', '')}")
    elif kind == "executor_question":
        question = " ".join((event.payload.get("question") or "").split())
        console.print(f"  [yellow]? executor asks:[/yellow] {question[:200]}")
    elif kind == "brain_self_answered":
        # The headline behavior: the brain answers the executor itself.
        answer = " ".join((event.payload.get("answer") or "").split())
        console.print(f"  [green]brain answers:[/green] {answer[:200]}")
    elif kind == "brain_escalated":
        question = " ".join((event.payload.get("question") or "").split())
        console.print(f"  [bold yellow]^ escalated to user:[/bold yellow] {question[:200]}")
    elif kind == "user_decided":
        answer = " ".join((event.payload.get("answer") or "").split())
        console.print(f"  [bold green]user decided:[/bold green] {answer[:200]}")
    elif kind == "step_reviewed":
        verdict = event.payload.get("verdict", "")
        color = {"accept": "green", "follow_up": "yellow", "revise_plan": "magenta"}.get(verdict, "white")
        console.print(f"  [{color}]review: {verdict}[/{color}]")
    elif kind == "memory_write":
        console.print(f"    [dim]memory += [{event.payload.get('kind')}] {event.payload.get('summary')}[/dim]")
    elif kind == "step_done":
        console.print(f"  [green]done:[/green] {event.payload.get('outcome')}")
    elif kind == "step_failed":
        console.print(f"  [red]failed:[/red] {event.payload.get('reason')}")
    elif kind == "escalation":
        console.print(f"\n[yellow]{event.message}[/yellow]")
    # "status" events are summarized by _print_planned_status after the loop.


def _print_planned_status(result) -> None:
    """Print the final terminal status of a planned run, distinctly per status."""
    status = result.status
    steps = len(result.plan.steps) if result.plan is not None else 0
    revisions = getattr(result, "revisions", 0)
    if status == STATUS_COMPLETED:
        console.print(
            f"\n[bold green]COMPLETED[/bold green] {steps} step(s) done "
            f"(escalations: {result.escalations}, plan revisions: {revisions})"
        )
    elif status == STATUS_PLANNING_FAILED:
        console.print("\n[bold red]PLANNING FAILED[/bold red] the brain produced no usable plan")
    elif status == STATUS_UNRESOLVED_ESCALATION:
        console.print(
            "\n[bold red]UNRESOLVED ESCALATION[/bold red] a product decision was needed but "
            "none could be obtained (no decision seam); nothing was guessed"
        )
    elif status == STATUS_ABORTED_BY_BRAIN:
        console.print("\n[bold red]ABORTED BY BRAIN[/bold red] goal deemed unreachable")
    elif status in (STATUS_ESCALATION_LIMIT, STATUS_REPEATED_STEP):
        # Plain-language, actionable -- not raw "escalation_limit" jargon.
        console.print(f"\n[bold red]STUCK[/bold red] {friendly_terminal_message(status)}")
    elif status == STATUS_MAX_STEPS:
        msg = friendly_terminal_message(status, max_total_steps=getattr(result, "max_total_steps", None))
        console.print(f"\n[yellow]STEP CEILING[/yellow] {msg}")
    elif status == STATUS_MAX_COST:
        msg = friendly_terminal_message(status)
        console.print(f"\n[yellow]COST CEILING[/yellow] {msg}")
    elif status == STATUS_DECLINED:
        console.print("\n[yellow]DECLINED[/yellow] plan not approved; nothing executed")
    else:
        console.print(f"\n[yellow]stopped: {status}[/yellow]")


def _print_run_error(exc: Exception) -> None:
    console.print(
        Panel(
            f"[red]Run failed:[/red] {exc}\n\n"
            "Relay reaches every model through OpenRouter. Make sure "
            "[bold]OPENROUTER_API_KEY[/bold] is set - copy "
            "[bold].env.example[/bold] to [bold].env[/bold] and add your key.",
            title="Relay run: error",
            border_style="red",
        )
    )


def _warn_if_dirty_git(root: str) -> None:
    """If ``root`` is a git repo with uncommitted changes, nudge to commit first.

    bash is not sandboxed, so git is the real undo net. Best-effort and silent
    on any error (e.g. git missing, or not a repo).
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:  # noqa: BLE001 — purely advisory; never block the run
        return
    if proc.returncode == 0 and proc.stdout.strip():
        console.print(
            "[yellow]note:[/yellow] the project has uncommitted git changes. Relay's bash "
            "is not sandboxed -- consider committing first so git is your undo net."
        )


def _interactive_approver(command: str, reason: str) -> bool:
    """Pause the loop and ask the user to approve a CONFIRM-category command."""
    console.print(
        Panel(
            f"[bold]Command:[/bold] {command}\n[bold]Why gated:[/bold] {reason}",
            title="Approval required (CONFIRM)",
            border_style="yellow",
        )
    )
    return typer.confirm("Run this command?", default=False)


def _print_step(step: StepResult) -> None:
    """Stream a single agent step to the console (action + result snippet)."""
    if step.kind == "parse_failure":
        console.print(f"[red]! parse failure[/red] - {step.observation}")
        return
    if step.kind == "done":
        return  # the final done line is printed by the caller
    console.print(f"[cyan]> {step.detail}[/cyan]")
    snippet = " ".join(step.observation.split())
    if not snippet:
        return
    # Refusals/denials must read as such, not blend into ordinary output.
    if snippet.startswith("BLOCKED by policy") or snippet.startswith("DENIED"):
        console.print(f"  [bold red]{snippet[:200]}[/bold red]")
    else:
        console.print(f"  [dim]{snippet[:200]}[/dim]")


def _print_telemetry(ledger: Ledger) -> None:
    """Render a per-role telemetry table with a totals line."""
    table = Table(title="Telemetry: tokens / cost / time")
    table.add_column("Role", style="bold")
    # Fold (wrap) long model slugs instead of ellipsis-truncating: Rich's default
    # truncation uses a unicode ellipsis that mojibakes on the legacy Windows
    # console. The brain-vs-hands split must read clearly, so keep it ASCII-safe.
    table.add_column("Model", overflow="fold")
    table.add_column("Prompt", justify="right")
    table.add_column("Completion", justify="right")
    table.add_column("Tokens", justify="right")
    table.add_column("Cost (USD)", justify="right")
    table.add_column("Time (s)", justify="right")

    for role, s in ledger.by_role().items():
        table.add_row(
            role,
            s.model,
            str(s.prompt_tokens),
            str(s.completion_tokens),
            str(s.total_tokens),
            "-" if s.cost_usd is None else f"${s.cost_usd:.6f}",
            f"{s.latency_s:.2f}",
        )

    total_cost = ledger.total_cost()
    total_tokens = sum(r.total_tokens for r in ledger.records)
    table.add_section()
    table.add_row(
        "TOTAL",
        "",
        "",
        "",
        str(total_tokens),
        "-" if total_cost is None else f"${total_cost:.6f}",
        f"{ledger.total_time():.2f}",
        style="bold",
    )
    console.print(table)

    # Parse-failure rate is a first-class model-quality signal, not a log line.
    pf = ledger.parse_failures
    style = "bold red" if pf else "dim"
    console.print(f"[{style}]parse failures: {pf}[/{style}]")


def _print_receipt(envelope: CostEnvelope, ledger: Ledger, *, status: str) -> None:
    """Print the A1 envelope receipt under the telemetry table."""
    for line in envelope.receipt_lines(ledger, status=status or ""):
        if line == "Receipt":
            console.print(f"[bold]{line}[/bold]")
        else:
            console.print(f"[dim]{line}[/dim]")


if __name__ == "__main__":
    app()
