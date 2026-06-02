"""The Relay CLI.

``relay models`` shows the role → model mapping; ``relay demo`` runs the
brain → hands seam once; ``relay run`` drives the two-role planner/executor
loop against a goal (``--solo <role>`` falls back to the single-model loop).
Every command prints telemetry, now naturally split brain vs hands.
"""

from __future__ import annotations

import subprocess

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from relay.config import load_models
from relay.loop import (
    STATUS_COMPLETED,
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
    STATUS_PLANNING_FAILED,
    Event,
    run_planned,
)
from relay.telemetry import Ledger

app = typer.Typer(
    help="Relay - a planner/executor coding agent on a model-agnostic OpenRouter seam.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


@app.command()
def models() -> None:
    """Print each role and the OpenRouter model resolved for it."""
    cfg = load_models()
    table = Table(title="Relay: roles -> models")
    table.add_column("Role", style="bold cyan")
    table.add_column("OpenRouter model", style="green")
    table.add_row("brain (planner)", cfg.brain)
    table.add_row("hands (executor)", cfg.hands)
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
) -> None:
    """Run the agent against a goal.

    Default: the two-role brain (planner) + hands (executor) architecture.
    Use --solo <role> for the single-model loop.
    """
    cfg = load_models()
    ledger = Ledger()
    approver = None if auto_approve else _interactive_approver
    _warn_if_dirty_git(root)

    if solo:
        _run_solo(goal, root, solo, max_steps, cfg, ledger, auto_approve, approver)
    else:
        _run_planned(goal, root, cfg, ledger, auto_approve, approver, confirm_plan)
    _print_telemetry(ledger)


def _run_solo(goal, root, role, max_steps, cfg, ledger, auto_approve, approver) -> None:
    """The v0.02 single-model loop, kept for comparison/debugging."""
    bash_policy = "auto-approve" if auto_approve else "interactive approval"
    console.print(
        Panel(
            f"[bold]{goal}[/bold]\nroot={root}  solo role={role}  max-steps={max_steps}  "
            f"bash policy={bash_policy}",
            title="Relay run (solo)",
            border_style="cyan",
        )
    )
    try:
        result = run_task(
            goal, root, role=role, max_steps=max_steps, models=cfg, ledger=ledger,
            on_step=_print_step, approver=approver, auto_approve=auto_approve,
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
    elif result.status == STATUS_PARSE_FAILURE_ABORT:
        console.print(
            "\n[bold red]aborted: too many consecutive parse failures[/bold red]"
        )
    else:
        console.print(f"\n[yellow]stopped: {result.status}[/yellow]")


def _run_planned(goal, root, cfg, ledger, auto_approve, approver, confirm_plan) -> None:
    """The two-role brain/hands architecture (the default)."""
    bash_policy = "auto-approve" if auto_approve else "interactive approval"
    console.print(
        Panel(
            f"[bold]{goal}[/bold]\nroot={root}\nbrain={cfg.brain}\nhands={cfg.hands}\n"
            f"bash policy={bash_policy}",
            title="Relay run (brain + hands)",
            border_style="cyan",
        )
    )
    try:
        result = run_planned(
            goal, root, models=cfg, ledger=ledger, client=None,
            approver=approver, auto_approve=auto_approve,
            on_event=_print_event,
            plan_gate=_confirm_plan_gate if confirm_plan else None,
        )
    except Exception as exc:  # noqa: BLE001 — surface any failure as a friendly message
        _print_run_error(exc)
        raise typer.Exit(code=1)
    _print_planned_status(result)


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
    elif kind == "replanned":
        steps = event.payload.get("steps", [])
        body = "\n".join(f"- {s}" for s in steps) or "(no steps)"
        console.print(Panel(body, title="Revised plan (brain)", border_style="magenta"))
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
    if status == STATUS_COMPLETED:
        console.print(
            f"\n[bold green]COMPLETED[/bold green] {steps} step(s) done "
            f"(escalations: {result.escalations})"
        )
    elif status == STATUS_PLANNING_FAILED:
        console.print("\n[bold red]PLANNING FAILED[/bold red] the brain produced no usable plan")
    elif status == STATUS_ABORTED_BY_BRAIN:
        console.print("\n[bold red]ABORTED BY BRAIN[/bold red] goal deemed unreachable")
    elif status == STATUS_ESCALATION_LIMIT:
        console.print(
            f"\n[bold red]ESCALATION LIMIT[/bold red] gave up after {result.escalations} replan(s)"
        )
    elif status == STATUS_MAX_STEPS:
        console.print("\n[yellow]MAX STEPS[/yellow] overall executor budget exhausted")
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


if __name__ == "__main__":
    app()
