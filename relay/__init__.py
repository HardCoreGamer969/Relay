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
"""

from __future__ import annotations

from relay.config import ModelConfig, load_models
from relay.loop import StepResult, TaskResult, run_task
from relay.models import ModelResult, call_model
from relay.orchestrator import Event, PlannedTaskResult, run_planned
from relay.planner import Plan, PlanStep, make_plan, project_digest, replan
from relay.policy import ALLOW, BLOCKED, CONFIRM, PolicyResult, classify
from relay.protocol import Action, ParseResult, parse
from relay.telemetry import CallRecord, Ledger
from relay.tools import PathEscapeError, ToolError, Tools

__version__ = "0.0.4"

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
    "__version__",
]
