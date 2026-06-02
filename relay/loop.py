"""The single-model agent loop.

v0.02 runs the loop with ONE role (default ``hands``). It does NOT split work
across brain/hands -- that planner/executor decomposition is a later milestone.

The loop is: call the model -> parse its text into actions (via the text
protocol) -> execute each action with the tools -> feed the results back as the
next message -> repeat until ``<done>`` or ``max_steps``. Parse failures are
recorded in the ledger and nudged back on track, with a bounded number of
consecutive retries before a clean abort.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from relay.config import ModelConfig
from relay.models import call_model
from relay.protocol import Action, parse
from relay.telemetry import Ledger
from relay.tools import ToolError, Tools

# How many consecutive parse failures to tolerate before aborting the run.
MAX_CONSECUTIVE_PARSE_FAILURES = 3

# The three distinct ways a run can end. Kept distinct because "died on the
# protocol" vs "merely ran out of turns" is the difference between a model that
# *can't* drive Relay and one that just needs more steps -- they must not look
# the same in the run-matrix later.
STATUS_COMPLETED = "completed"  # the model emitted <done>
STATUS_MAX_STEPS = "max_steps"  # ran out of turns without <done>
STATUS_PARSE_FAILURE_ABORT = "parse_failure_abort"  # hit consecutive-parse-failure limit

SYSTEM_PROMPT = """\
You are Relay, an autonomous coding agent working inside a single project directory.

You reach the goal by taking ONE step at a time. After each step you are shown
the result; use it to decide your next step.

Tools available to you:
- read a file's full contents.
- list a directory's entries.
- grep a regex pattern across a file or directory; you get matching lines with line numbers.
- edit a file by writing its FULL new contents; the file is replaced and parent directories are created.
- bash to run a shell command whose working directory is the project root; you get stdout, stderr, and the exit code.

Express EVERY action using these EXACT text tags. Never describe an action in
prose -- emit the tag. You may emit more than one tag, but prefer one step at a time:
  <thinking>your private reasoning</thinking>   (optional; ignored by the executor)
  <read path="relative/path"/>
  <list path="relative/path"/>
  <grep pattern="regex" path="relative/path"/>
  <edit path="relative/path">FULL NEW FILE CONTENTS</edit>
  <bash>command</bash>
  <done>one-line summary of what was accomplished</done>

Rules:
- Paths are relative to the project root; you cannot escape it.
- Observe each result before acting again.
- When the goal is fully met, emit <done>...</done> and nothing else.
"""


@dataclass
class StepResult:
    """One executed step in the transcript."""

    kind: str  # action kind ("read"/"edit"/...), "done", or "parse_failure"
    detail: str  # short human-readable description of the action
    observation: str  # the result text that was fed back to the model


@dataclass
class TaskResult:
    """The outcome of a :func:`run_task` run."""

    goal: str
    steps: list[StepResult] = field(default_factory=list)
    # Terminal state. Defaults to ``max_steps`` -- the state a run is in if it
    # falls out of the loop having neither finished nor aborted.
    status: str = STATUS_MAX_STEPS
    done_summary: str | None = None
    ledger: Ledger | None = None

    @property
    def done(self) -> bool:
        """Back-compat: True iff the run completed by emitting ``<done>``."""
        return self.status == STATUS_COMPLETED


def _describe(action: Action) -> str:
    """A short, prose-free label for an action (for the transcript/console)."""
    if action.kind == "read":
        return f'read path="{action.path}"'
    if action.kind == "list":
        return f'list path="{action.path}"'
    if action.kind == "grep":
        return f'grep pattern="{action.pattern}" path="{action.path}"'
    if action.kind == "edit":
        return f'edit path="{action.path}"'
    if action.kind == "bash":
        return f"bash: {action.content}"
    if action.kind == "done":
        return f"done: {action.content}"
    return action.kind


def _execute(tools: Tools, action: Action) -> str:
    """Run a single action against the tools, returning an observation string."""
    try:
        if action.kind == "read":
            return tools.read(action.path or "")
        if action.kind == "list":
            return tools.list(action.path or ".")
        if action.kind == "grep":
            return tools.grep(action.pattern or "", action.path or "")
        if action.kind == "edit":
            return tools.edit(action.path or "", action.content or "")
        if action.kind == "bash":
            return tools.bash(action.content or "")
        return f"error: unknown action {action.kind!r}"
    except ToolError as exc:
        return f"error: {exc}"
    except Exception as exc:  # noqa: BLE001 -- feed any failure back to the model
        return f"error: {exc}"


def run_task(
    goal: str,
    project_root: str | Path,
    *,
    role: str = "hands",
    max_steps: int = 20,
    models: ModelConfig | None = None,
    ledger: Ledger | None = None,
    client: Any | None = None,
    on_step: Callable[[StepResult], None] | None = None,
) -> TaskResult:
    """Drive the single-model agent loop until ``<done>`` or ``max_steps``.

    Args:
        goal: The task to accomplish.
        project_root: Directory the agent's tools are confined to.
        role: Which model role drives the loop (single role; default ``hands``).
        max_steps: Maximum model turns before stopping.
        models: Role->model config (defaults to :func:`relay.config.load_models`).
        ledger: Telemetry ledger (created if not supplied).
        client: OpenRouter client; injected in tests to stay network-free.
        on_step: Optional callback invoked with each :class:`StepResult` as it
            happens, so callers (e.g. the CLI) can stream progress live.
    """
    tools = Tools(Path(project_root))
    ledger = ledger if ledger is not None else Ledger()
    result = TaskResult(goal=goal, ledger=ledger)

    messages: list[dict[str, str]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Goal: {goal}\n\nBegin. Emit your first action."},
    ]

    def emit(step: StepResult) -> None:
        result.steps.append(step)
        if on_step is not None:
            on_step(step)

    consecutive_parse_failures = 0

    for _ in range(max_steps):
        reply = call_model(
            role, messages, models=models, ledger=ledger, client=client
        ).text
        messages.append({"role": "assistant", "content": reply})

        parsed = parse(reply)

        if parsed.is_parse_failure:
            ledger.record_parse_failure()
            consecutive_parse_failures += 1
            snippet = " ".join(reply.split())[:200]
            emit(StepResult(kind="parse_failure", detail="no valid action", observation=snippet))
            if consecutive_parse_failures >= MAX_CONSECUTIVE_PARSE_FAILURES:
                result.status = STATUS_PARSE_FAILURE_ABORT
                break  # abort cleanly
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "No valid action was found in your message. Re-emit your next "
                        "step using the protocol tags exactly, e.g. "
                        '<read path="..."/>, <edit path="...">...</edit>, '
                        "<bash>...</bash>, or <done>...</done>."
                    ),
                }
            )
            continue

        consecutive_parse_failures = 0
        observations: list[str] = []
        for action in parsed.actions:
            if action.kind == "done":
                result.status = STATUS_COMPLETED
                result.done_summary = action.content or ""
                emit(StepResult(kind="done", detail=_describe(action), observation=""))
                break
            observation = _execute(tools, action)
            emit(StepResult(kind=action.kind, detail=_describe(action), observation=observation))
            observations.append(f"[{_describe(action)}]\n{observation}")

        if result.done:
            break

        messages.append(
            {"role": "user", "content": "\n\n".join(observations) if observations else "(no output)"}
        )

    return result
