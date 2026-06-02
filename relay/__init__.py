"""Relay — a planner/executor coding agent built on a model-agnostic OpenRouter seam.

- v0.01 shipped the model layer: the single seam (``call_model``) every later
  part of the system is built on, with telemetry recorded on every call.
- v0.02 adds the text action protocol, the executor's tools, and a
  single-model agent loop (``run_task``) -- Relay now *does* work, not just
  describes it. Actions are plain-text tags Relay parses itself (never a
  provider's native tool-calling API).
"""

from __future__ import annotations

from relay.config import ModelConfig, load_models
from relay.loop import StepResult, TaskResult, run_task
from relay.models import ModelResult, call_model
from relay.protocol import Action, ParseResult, parse
from relay.telemetry import CallRecord, Ledger
from relay.tools import PathEscapeError, ToolError, Tools

__version__ = "0.0.2"

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
    "__version__",
]
