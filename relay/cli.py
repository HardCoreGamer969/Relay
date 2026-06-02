"""The Relay CLI.

``relay models`` shows the role → model mapping; ``relay demo`` runs the
brain → hands seam once; ``relay run`` drives the single-model agent loop
against a goal. Every command prints telemetry (now including parse failures).
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from relay.config import load_models
from relay.loop import StepResult, run_task
from relay.models import call_model
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
        ..., "--goal", "-g", help="The goal for the agent loop to accomplish."
    ),
    root: str = typer.Option(
        ".", "--root", help="Project root the agent's tools are confined to."
    ),
    max_steps: int = typer.Option(
        20, "--max-steps", help="Maximum model turns before stopping."
    ),
    role: str = typer.Option(
        "hands", "--role", help="Which model role drives the loop (single role)."
    ),
) -> None:
    """Run the single-model agent loop against a goal, streaming each step."""
    cfg = load_models()
    ledger = Ledger()

    console.print(
        Panel(
            f"[bold]{goal}[/bold]\nroot={root}  role={role}  max-steps={max_steps}",
            title="Relay run",
            border_style="cyan",
        )
    )

    try:
        result = run_task(
            goal,
            root,
            role=role,
            max_steps=max_steps,
            models=cfg,
            ledger=ledger,
            on_step=_print_step,
        )
    except Exception as exc:  # noqa: BLE001 — surface any failure as a friendly message
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
        raise typer.Exit(code=1)

    if result.done:
        console.print(f"\n[bold green]done:[/bold green] {result.done_summary}")
    else:
        console.print(
            "\n[yellow]stopped without <done> (hit max-steps or consecutive "
            "parse failures)[/yellow]"
        )
    _print_telemetry(ledger)


def _print_step(step: StepResult) -> None:
    """Stream a single agent step to the console (action + result snippet)."""
    if step.kind == "parse_failure":
        console.print(f"[red]! parse failure[/red] - {step.observation}")
        return
    if step.kind == "done":
        return  # the final done line is printed by the caller
    console.print(f"[cyan]> {step.detail}[/cyan]")
    snippet = " ".join(step.observation.split())
    if snippet:
        console.print(f"  [dim]{snippet[:200]}[/dim]")


def _print_telemetry(ledger: Ledger) -> None:
    """Render a per-role telemetry table with a totals line."""
    table = Table(title="Telemetry: tokens / cost / time")
    table.add_column("Role", style="bold")
    table.add_column("Model")
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
