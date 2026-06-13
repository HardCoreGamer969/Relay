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
- v0.08 (A of B) makes planning a **conversation** (``plan_conversationally``):
  the brain assesses scope, proposes or asks, the user reacts in plain language,
  and on commit hands a ``Plan`` to ``run_planned``. The **assumption dial**
  (``resolve_assumption_level``) is a user-owned global bias on every
  assume-vs-ask decision -- the conversation AND ``answer_or_escalate``.
- v0.08 (B of B) fuses planning and execution into ONE continuous **transcript**
  (``Transcript`` of ``Turn`` values): the planning dialogue and the mid-run
  escalations are turns in the same thread, so a product decision asked mid-run
  reads as a continuation of the conversation, not a context-less popup. Plan
  memory now DERIVES from the transcript (``record_decision`` is transcript-first,
  the memory entry's provenance links back to the turn). The transcript compacts
  toward **readability** (``compact_transcript`` / ``render_for_brain`` -- recent
  verbatim, older folded into a readable narrative), distinct from plan memory's
  dense compaction, window-bounded for brain reads, run as a post-execution pass.
- v0.0.10 polishes the transcript before the TUI: a ``proposal`` turn now carries
  a plain one/two-sentence HEADLINE (emitted in the same generation as the plan,
  derived from it -- no extra brain call), not the full executor spec, so
  scroll-back stays readable; and the closing result turn no longer claims it
  "built everything" when a step failed and was replanned around.
- v0.0.11 (TUI 1 of 2) ships the **sync<->async bridge** and a minimal two-pane
  Textual chat (``relay tui``). ``EngineBridge`` parks the engine's blocking
  seams on a thread-safe handoff (``UiRequest``) so the engine -- running on
  ``EngineRunner``'s worker thread -- never knows it is talking to a TUI;
  ``InputRouter`` routes the one input box by what the engine awaits. The
  money-leak guards: an additive step-boundary ``cancel_check`` in
  ``run_planned`` (terminal status ``cancelled``) and a quit path that cancels
  and JOINS the worker. The TUI render path is unicode-clean; the plain CLI is
  untouched. Onboarding/model-picker/experience dial are TUI part 2.
- v0.0.12 is a TUI **visual polish** pass (look layer only -- no engine/bridge
  change): a genuinely separate **welcome state** (the letterspaced ``RELAY``
  block wordmark hero, a rotating greeting, the brain/hands pairing promoted as
  identity, a dim hint) that **glitch/datamosh-transitions** into the two
  working panes on the first goal. Animations route through one mode-gated
  chokepoint (``"short"`` live, ``"off"`` instant, ``"long"`` stubbed) so the
  next milestone's config/launch-counter slots in without a refactor. The
  handoff is purely visual -- the run kicks off immediately, never gated on an
  animation.
- v0.0.13 makes Relay genuinely **multi-provider** (the backend milestone under
  the next TUI picker). Three additions: (1) a **model catalog** (``relay/catalog.py``)
  that fetches model metadata + pricing from an external source (default
  ``models.dev``), validates/caches it, and serves cost/capabilities/context-limit
  lookups -- with a fetch -> fresh-cache -> stale-cache -> bundled-fallback chain
  so a network blip never bricks Relay or zeros cost; (2) thin **provider profiles**
  (``relay/providers.py`` -- ``{id, base_url, key_env}``) selected **per role**
  (``RELAY_BRAIN_PROVIDER`` / ``RELAY_HANDS_PROVIDER``, default ``openrouter`` so
  all prior behavior is unchanged); and (3) **DeepSeek direct** as the first
  non-OpenRouter provider, with **catalog-driven cost** that respects DeepSeek's
  cache hit/miss split (``hit*cache_read + miss*input + out*output``) instead of a
  naive single rate. Thinking mode is off by default and per-role-toggleable.
  ``relay doctor`` is now provider-aware (preflights each role against its own
  provider, reports the catalog source and per-role context window). The text
  protocol stays the universal execution mechanism; the OpenRouter path is
  byte-for-byte unchanged. The in-TUI key entry / model picker sits on top of
  this and is the next milestone.
"""

from __future__ import annotations

from relay.bridge import (
    BridgeCancelled,
    EngineBridge,
    EngineRunner,
    InputRouter,
    InputState,
    RunOutcome,
    SubmitOutcome,
    UiRequest,
)
from relay.catalog import (
    Catalog,
    Cost,
    Limit,
    Model,
    Provider,
    get_catalog,
    load_catalog,
    reset_catalog_cache,
)
from relay.config import (
    ASSUMPTION_LEVELS,
    DEFAULT_ASSUMPTION_LEVEL,
    ModelConfig,
    assumption_directive,
    load_env,
    load_models,
    resolve_assumption_level,
)
from relay.context import DEFAULT_CONTEXT_WINDOW, resolve_context_window
from relay.providers import (
    DEFAULT_PROVIDER,
    ProviderProfile,
    known_providers,
    resolve_provider,
)
from relay.conversation import ConversationResult, ScopeAssessment, plan_conversationally
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
    STATUS_CANCELLED,
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
from relay.transcript import (
    Transcript,
    Turn,
    compact_transcript,
    record_decision,
    render_for_brain,
)

__version__ = "0.0.13"

__all__ = [
    # v0.01 -- model layer
    "call_model",
    "ModelResult",
    "ModelConfig",
    "load_models",
    "load_env",
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
    # v0.08 (A of B) -- conversational planning + the assumption dial
    "plan_conversationally",
    "ConversationResult",
    "ScopeAssessment",
    "resolve_assumption_level",
    "assumption_directive",
    "ASSUMPTION_LEVELS",
    "DEFAULT_ASSUMPTION_LEVEL",
    # v0.08 (B of B) -- the continuous transcript + readability compaction
    "Transcript",
    "Turn",
    "record_decision",
    "compact_transcript",
    "render_for_brain",
    # v0.0.11 (TUI 1 of 2) -- the sync<->async bridge + step-boundary cancel
    "EngineBridge",
    "EngineRunner",
    "RunOutcome",
    "UiRequest",
    "BridgeCancelled",
    "InputRouter",
    "InputState",
    "SubmitOutcome",
    "STATUS_CANCELLED",
    # v0.0.13 -- model catalog + provider profiles (multi-provider seam)
    "Catalog",
    "Cost",
    "Limit",
    "Model",
    "Provider",
    "load_catalog",
    "get_catalog",
    "reset_catalog_cache",
    "ProviderProfile",
    "resolve_provider",
    "known_providers",
    "DEFAULT_PROVIDER",
    "__version__",
]
