"""Relay — a planner/executor coding agent built on a model-agnostic OpenRouter seam.

- v0.01 shipped the model layer: the single seam (``call_model``) every later
  part of the system is built on, with telemetry recorded on every call.
- v0.02 adds the text action protocol, the executor's tools, and a
  single-model agent loop (``run_task``) -- Relay now *does* work, not just
  describes it. Actions are plain-text tags Relay parses itself (never a
  provider's native tool-calling API).
- v0.03 adds the command-policy guardrail: ``bash`` commands are classified
  (``BLOCKED`` / ``CONFIRM`` / ``ALLOW``) and gated behind approval. It is a
  best-effort speed bump, NOT a security sandbox (real isolation is v0.95).
- v0.04 adds the two-role architecture Relay is named for: a brain (planner)
  plans the ordered work up front and the hands (executor) carry out each step
  in a narrow context, with the brain re-engaging only on escalation
  (``run_planned``). The single-model ``run_task`` is kept for comparison.
- v0.05 persists each run as a structured ``RunRecord`` (JSONL at
  ``.relay/runs.jsonl``) so runs are comparable over time, and adds a
  ``relay doctor`` slug preflight. The persisted schema is the durable floor
  the run-matrix (v0.1) will read.
- v0.06 (1 of 2) adds within-run **plan memory** (``PlanMemory`` of
  dual-fidelity ``MemoryEntry`` values) and **context-window awareness**
  (``resolve_context_window``) so memory is budgeted, sliced, and compressed to
  fit any brain -- from a 200K frontier model down to an 8K local one.
- v0.06 (2 of 2) closes the loop: ``run_planned`` is now autonomous -- the brain
  SUPERVISES the executor at step boundaries (``review_step``), ANSWERS its
  ``<question>``s itself or ESCALATES product decisions (``answer_or_escalate``),
  LEARNS into memory, and EVOLVES the plan (``evolve_plan``). The human is no
  longer in the middle of the loop.
"""

from __future__ import annotations

from relay.config import ModelConfig, load_models
from relay.context import DEFAULT_CONTEXT_WINDOW, resolve_context_window
from relay.loop import StepResult, TaskResult, run_task
from relay.memory import (
    MemoryEntry,
    PlanMemory,
    estimate_tokens,
    memory_budget,
    small_window_warning,
)
from relay.models import ModelResult, call_model
from relay.orchestrator import (
    STATUS_UNRESOLVED_ESCALATION,
    Event,
    PlannedTaskResult,
    run_planned,
)
from relay.planner import (
    Plan,
    PlanStep,
    Resolution,
    StepReview,
    answer_or_escalate,
    evolve_plan,
    make_plan,
    project_digest,
    replan,
    review_step,
)
from relay.policy import ALLOW, BLOCKED, CONFIRM, PolicyResult, classify
from relay.protocol import Action, ParseResult, parse
from relay.runlog import (
    SCHEMA_VERSION,
    RunRecord,
    append_record,
    build_record,
    default_log_path,
    load_records,
)
from relay.telemetry import CallRecord, Ledger
from relay.tools import PathEscapeError, ToolError, Tools

__version__ = "0.0.7"

__all__ = [
    # v0.01 -- model layer
    "call_model",
    "ModelResult",
    "ModelConfig",
    "load_models",
    "Ledger",
    "CallRecord",
    # v0.02 -- protocol, tools, loop
    "parse",
    "Action",
    "ParseResult",
    "Tools",
    "ToolError",
    "PathEscapeError",
    "run_task",
    "StepResult",
    "TaskResult",
    # v0.03 -- command policy
    "classify",
    "PolicyResult",
    "BLOCKED",
    "CONFIRM",
    "ALLOW",
    # v0.04 -- brain/hands orchestration
    "run_planned",
    "PlannedTaskResult",
    "Event",
    "make_plan",
    "replan",
    "Plan",
    "PlanStep",
    "project_digest",
    # v0.05 -- durable run records
    "RunRecord",
    "build_record",
    "append_record",
    "load_records",
    "default_log_path",
    "SCHEMA_VERSION",
    # v0.06 (1 of 2) -- plan memory + context-window awareness
    "PlanMemory",
    "MemoryEntry",
    "memory_budget",
    "small_window_warning",
    "estimate_tokens",
    "resolve_context_window",
    "DEFAULT_CONTEXT_WINDOW",
    # v0.06 (2 of 2) -- autonomous brain behaviors
    "review_step",
    "answer_or_escalate",
    "evolve_plan",
    "StepReview",
    "Resolution",
    "STATUS_UNRESOLVED_ESCALATION",
    "__version__",
]
