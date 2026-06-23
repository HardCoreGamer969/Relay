"""The single-model agent loop.

``run_task`` runs the loop with ONE role (default ``hands``): it does NOT split
work across brain/hands. The two-role planner/executor architecture lives in
``relay.orchestrator`` (v0.04) and reuses this module's shared helpers
(``execute_action`` / ``describe_action``); ``run_task`` is kept intact as the
single-model loop, still useful for comparison and via ``relay run --solo``.

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
from relay.tools import PatchError, ToolError, Tools, parse_patch

# How many consecutive parse failures to tolerate before aborting the run.
MAX_CONSECUTIVE_PARSE_FAILURES = 3


class ParseFailureTracker:
    """Tracks consecutive parse failures and signals when the limit is exceeded.

    Shared by the solo loop (``run_task``), the executor mini-loop
    (``_run_executor_step``), and the brain investigation loop (``investigate``)
    so the three don't drift on the nudge/abort threshold. Each ``record()``
    call increments the counter (and the ledger's parse-failure count); a
    successful turn calls ``reset()``. The ``exceeded`` property signals when
    the loop should abort.
    """

    def __init__(
        self,
        max_failures: int = MAX_CONSECUTIVE_PARSE_FAILURES,
        ledger: Ledger | None = None,
    ) -> None:
        self._count = 0
        self._max = max_failures
        self._ledger = ledger

    def record(self) -> None:
        """Record a parse failure (increments the counter + the ledger)."""
        self._count += 1
        if self._ledger is not None:
            self._ledger.record_parse_failure()

    def reset(self) -> None:
        """Reset the counter after a successful turn."""
        self._count = 0

    @property
    def exceeded(self) -> bool:
        """True when the consecutive-failure limit has been reached."""
        return self._count >= self._max

    @property
    def count(self) -> int:
        """The current consecutive-failure count."""
        return self._count

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
- glob to find files by pattern (e.g. **/*.py); prefer it over bash find/ls.
- grep a regex pattern across a file or directory; you get matching lines with line numbers.
- write a whole file's contents (create or fully replace); parent directories are created.
- edit a file by writing its FULL new contents; the file is replaced and parent directories are created.
- apply_patch to make targeted multi-location / multi-file / rename / delete changes in ONE atomic patch (OpenCode envelope; all-or-nothing).
- mkdir to create a directory (and parents) cross-platform, without a shell -- no `mkdir -p`.
- webfetch a URL and get back its readable text.
- bash to run a shell command whose working directory is the project root; you get stdout, stderr, and the exit code. Some destructive commands are refused by policy (you will see "BLOCKED by policy: ...") or require user approval (you will see "DENIED ..." if not approved) -- when a command is refused, adapt and find another way rather than re-emitting it verbatim.

Express EVERY action using these EXACT text tags. Never describe an action in
prose -- emit the tag. You may emit more than one tag, but prefer one step at a time:
  <thinking>your private reasoning</thinking>   (optional; ignored by the executor)
  <read path="relative/path"/>
  <list path="relative/path"/>
  <glob pattern="**/*.py"/>
  <grep pattern="regex" path="relative/path"/>
  <write path="relative/path">FULL FILE CONTENTS</write>
  <edit path="relative/path">FULL NEW FILE CONTENTS</edit>
  <apply_patch>*** Begin Patch ... *** End Patch</apply_patch>
  <mkdir path="relative/path"/>
  <webfetch url="https://..."/>
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


def describe_action(action: Action) -> str:
    """A short, prose-free label for an action (for the transcript/console).

    Public so the planner/orchestrator (v0.04) can reuse it.
    """
    if action.kind == "read":
        return f'read path="{action.path}"'
    if action.kind == "list":
        return f'list path="{action.path}"'
    if action.kind == "grep":
        return f'grep pattern="{action.pattern}" path="{action.path}"'
    if action.kind == "edit":
        return f'edit path="{action.path}"'
    if action.kind == "write":
        return f'write path="{action.path}"'
    if action.kind == "glob":
        base = f' path="{action.path}"' if action.path and action.path != "." else ""
        return f'glob pattern="{action.pattern}"{base}'
    if action.kind == "apply_patch":
        return "apply_patch"
    if action.kind == "webfetch":
        return f'webfetch url="{action.url}"'
    if action.kind == "mkdir":
        return f'mkdir path="{action.path}"'
    if action.kind == "bash":
        return f"bash: {action.content}"
    if action.kind == "done":
        return f"done: {action.content}"
    if action.kind == "finding":
        return f"finding: {action.content}"
    return action.kind


def execute_action(tools: Tools, action: Action) -> str:
    """Run a single action against the tools, returning an observation string.

    Public so the planner/orchestrator (v0.04) can reuse it.
    """
    try:
        if action.kind == "read":
            return tools.read(action.path or "")
        if action.kind == "list":
            return tools.list(action.path or ".")
        if action.kind == "grep":
            return tools.grep(action.pattern or "", action.path or "")
        if action.kind == "edit":
            return tools.edit(action.path or "", action.content or "")
        if action.kind == "write":
            return tools.write(action.path or "", action.content or "")
        if action.kind == "glob":
            return tools.glob(action.pattern or "", action.path or ".")
        if action.kind == "apply_patch":
            return tools.apply_patch(action.content or "")
        if action.kind == "webfetch":
            return tools.webfetch(action.url or "")
        if action.kind == "mkdir":
            return tools.mkdir(action.path or "")
        if action.kind == "bash":
            return tools.bash(action.content or "")
        if action.kind == "finding":
            # A <finding> is a note to the planner, not a tool op. It has no file
            # effect and never touches the filesystem; the orchestrator's executor
            # loop routes it to shared memory. Here (the solo loop / brain
            # investigation) it is simply acknowledged, never executed.
            return "(finding noted)"
        return f"error: unknown action {action.kind!r}"
    except ToolError as exc:
        return f"error: {exc}"
    except Exception as exc:  # noqa: BLE001 -- feed any failure back to the model
        return f"error: {exc}"


# --- the read-before-edit guard (v0.0.23, the hands/executor only) ------------
#
# The hands may not <edit> an EXISTING file it has not read (with a still-current
# read) earlier in this run. Two purposes: (1) prevent blind edits -- changing a
# file from a stale/assumed picture; and (2) let the hands catch a hallucinating
# brain -- if the step assumes the file looks like X but it is Z, the hands (now
# forced to have the real contents in view) can report the discrepancy rather than
# applying a wrong change.
#
# Freshness is CONTENT-hash based, not step-scoped: a read stays valid as long as
# the file's contents are unchanged (so unchanged files need no pointless re-read),
# but ANY edit invalidates the file's recorded read, so the next edit needs a fresh
# read reflecting the new state. The refusal is a NORMAL, recoverable observation
# (not a parse failure or step abort): the loop continues so the hands can read-then
# -edit, or speak up about a mismatch.


def _read_before_edit_refusal(path: str) -> str:
    return (
        f'Refused: you must read "{path}" before editing it -- its current contents '
        f'may differ from what you expect. Use <read path="{path}"/> first.'
    )


def read_is_current(tools: Tools, path: str, reads: dict[str, str]) -> bool:
    """True iff ``path`` was read this run AND its contents are unchanged since the
    read (the recorded content hash still matches what is on disk)."""
    recorded = reads.get(tools.canonical(path))
    if recorded is None:
        return False
    return tools.content_hash(path) == recorded


def record_read(tools: Tools, path: str, reads: dict[str, str]) -> None:
    """Record ``path``'s current content hash, marking a later edit of it allowed.
    A no-op if the file cannot be hashed (missing/directory)."""
    digest = tools.content_hash(path)
    if digest is not None:
        reads[tools.canonical(path)] = digest


def invalidate(tools: Tools, path: str, reads: dict[str, str]) -> None:
    """Drop any recorded read for ``path`` (a write changed it, so a subsequent edit
    must re-read to reflect the new state)."""
    reads.pop(tools.canonical(path), None)


def guarded_execute_action(tools: Tools, action: Action, reads: dict[str, str] | None) -> str:
    """:func:`execute_action` plus the read-before-edit guard (hands/executor only).

    - ``<read>`` of an existing file: execute, then record its content hash so a
      later ``<edit>``/``<write>`` is allowed.
    - ``<edit>``/``<write>`` to a NEW file (path absent): allowed unconditionally
      (creation is a core operation -- there is nothing to read).
    - ``<edit>``/``<write>`` to an EXISTING file with no current read: REFUSED -- the
      file is left untouched and a recoverable observation tells the hands to read it.
    - ``<edit>``/``<write>`` to an existing file with a current read: applied, then the
      read is invalidated (the write changed the file).

    ``reads is None`` disables the guard (plain :func:`execute_action`) -- the solo
    single-model loop does not track reads.
    """
    if reads is None:
        return execute_action(tools, action)
    if action.kind in ("edit", "write"):
        # Both write a whole file. New file -> allowed (nothing to read); existing file
        # without a current read -> refused (don't blind-clobber a file you haven't seen).
        path = action.path or ""
        if tools.exists(path) and not read_is_current(tools, path, reads):
            return _read_before_edit_refusal(path)  # do NOT write; recoverable observation
        observation = execute_action(tools, action)
        invalidate(tools, path, reads)  # the write changed the file: a later edit needs a fresh read
        return observation
    if action.kind == "apply_patch":
        try:
            sections = parse_patch(action.content or "")
        except PatchError as exc:
            return f"apply_patch failed: {exc}"  # malformed: clear, recoverable observation
        # Per-section read guard: every Update target that EXISTS needs a current read
        # (same rule as edit/write); Add is exempt, Delete needs existence (checked in
        # Tools.apply_patch). Refuse the WHOLE patch if any Update target is unread.
        for sec in sections:
            if sec.op == "update" and tools.exists(sec.path) and not read_is_current(tools, sec.path, reads):
                return _read_before_edit_refusal(sec.path)
        observation = tools.apply_patch(action.content or "")
        if observation.startswith("applied patch"):
            # The patch changed these files: a later edit of any of them needs a fresh read.
            for sec in sections:
                invalidate(tools, sec.path, reads)
                if sec.move_to:
                    invalidate(tools, sec.move_to, reads)
        return observation
    observation = execute_action(tools, action)
    if action.kind == "read" and not observation.startswith("error:"):
        record_read(tools, action.path or "", reads)
    return observation


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
    approver: Callable[[str, str], bool] | None = None,
    auto_approve: bool = False,
    bash_timeout_s: float | None = 120.0,
) -> TaskResult:
    """Drive the single-model agent loop until ``<done>`` or ``max_steps``.

    Args:
        goal: The task to accomplish.
        project_root: Directory the agent's tools are confined to.
        role: Which model role drives the loop (single role; default ``hands``).
        max_steps: Maximum model turns before stopping.
        models: Role->model config (defaults to :func:`load_models`).
        ledger: Telemetry ledger (created if not supplied).
        client: OpenRouter client; injected in tests to stay network-free.
        on_step: Optional callback invoked with each :class:`StepResult` as it
            happens, so callers (e.g. the CLI) can stream progress live.
        approver: Decides ``CONFIRM``-category bash commands ``(command, reason)
            -> bool``. None + ``auto_approve`` False denies them (safe default).
        auto_approve: Approve ``CONFIRM`` bash commands without asking. Never
            affects ``BLOCKED`` commands, which are always refused.
        bash_timeout_s: Timeout for bash commands in seconds. ``None`` disables
            the timeout (unbounded). Default 120s.
    """
    tools = Tools(Path(project_root), approver=approver, auto_approve=auto_approve,
                  bash_timeout_s=bash_timeout_s)
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
                emit(StepResult(kind="done", detail=describe_action(action), observation=""))
                break
            observation = execute_action(tools, action)
            emit(StepResult(kind=action.kind, detail=describe_action(action), observation=observation))
            observations.append(f"[{describe_action(action)}]\n{observation}")

        if result.done:
            break

        messages.append(
            {"role": "user", "content": "\n\n".join(observations) if observations else "(no output)"}
        )

    return result
