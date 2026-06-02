"""Relay — a planner/executor coding agent built on a model-agnostic OpenRouter seam.

v0.01 ships only the model layer: the single seam (``call_model``) every later
part of the system is built on, with telemetry recorded on every call.
"""

from __future__ import annotations

from relay.config import ModelConfig, load_models
from relay.models import ModelResult, call_model
from relay.telemetry import CallRecord, Ledger

__version__ = "0.0.1"

__all__ = [
    "call_model",
    "ModelResult",
    "ModelConfig",
    "load_models",
    "Ledger",
    "CallRecord",
    "__version__",
]
