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
- v0.0.14 is a one-line **bug fix** with the test that was missing: Relay now loads
  a project ``.env`` from the **current working directory** (``config.load_env`` via
  ``find_dotenv(usecwd=True)``), not from Relay's install/module tree. The shipped
  ``load_dotenv()`` resolved relative to the caller module's file, so under a global/
  editable install a project ``.env`` was silently ignored -- breaking the "swap via
  env, never code" promise with no error. Process env vars still override the file
  (``override=False``); an absent ``.env`` is harmless. Config key resolution is
  unchanged -- only *when/where* the file is loaded.
- v0.0.15 is a TUI **render-layer polish** pass (no engine/bridge/token change):
  (1) the input placeholder rotates through a small set and is **state-aware**
  (idle vs awaiting-reaction/decision/approval); (2) the **proposal split** -- the
  conversation pane shows only the human-readable headline + surfaced assumptions
  while the full numbered executor plan moves to the activity pane (dual fidelity,
  pre-commit AND in scroll-back); and (3) the activity pane surfaces the
  **brain<->hands exchange** as an attributed, scrollable feed built ENTIRELY from
  events the engine already emits (``describe_event_for_activity``) -- adding zero
  model calls / token spend, codified by a guard test. Pure presentation: nothing
  here narrates or summarizes via a generation.
- v0.0.16 adds **provider configuration + secrets** (beta-enablement): a persistent
  global store in two deliberately separate files under the OS user-config dir --
  ``config.json`` (inspectable selections + reserved picker sockets;
  :mod:`relay.store`) and ``auth.json`` (credentials, ``0o600``, isolated in
  :mod:`relay.secrets`; a key never touches config/logs/output). Precedence
  preserves the env/.env workflow as highest: models resolve env > config.json >
  default (:func:`~relay.config.resolve_role_field`), keys resolve env-key >
  auth.json (:func:`~relay.secrets.resolve_key`). Provider profiles gain a
  ``discovery`` mode -- OpenRouter is ``manual`` (type-a-slug, validated live),
  DeepSeek is ``list`` (enumerates live via ``/models`` so deprecations
  self-correct). A ``relay config`` CLI group (show / set-role / set-key [no echo]
  / remove-key / list-models) and an in-TUI setup screen (ctrl+s: masked key entry,
  per-role model pick, thinking toggle) let a beta user configure everything in the
  app; an empty first-run is guided into setup (offered-but-prominent), while a
  user with working env vars/keys goes straight to chat. The picker's ``cost_bias``
  / ``recommendations_source`` sockets are reserved but inert.
- v0.0.17 adds a **dialog-driven slash-command** control plane to the TUI: typing
  ``/`` in the prompt opens a filterable popover (:data:`relay.tui.COMMANDS`); every
  command opens a DIALOG or runs a clean no-arg action -- NONE parse inline
  arguments, and no command (especially ``/key``) ever reads a value out of the
  prompt text. One generic ``SelectDialog`` is the primitive every list command
  (``/help`` ``/model`` ``/config`` ``/doctor`` ``/runs`` ``/assume``) opens; a
  ``TextEntryDialog`` (masked for ``/key``, plain for a manual slug) is the entry
  primitive. Slash commands are a thin front door that LAUNCHES the existing
  v0.0.16 flows (masked key entry, live model listing, ``validate_model``,
  ``persist_role``, ``secrets.set_key``, the doctor/runs logic) -- reused, never
  forked. The popover is gated to the IDLE input state so the engine/InputRouter
  is undisturbed; ``/clear`` is disabled mid-run. First-run guidance is now
  slash-native (``/key`` to start, ``/help`` for all commands).
- v0.0.18 adds a reusable **``SegmentedControl``** primitive (a horizontal
  choose-one toggle: left/right with wrap, Enter commits, Esc cancels -- the
  analog of ``SelectDialog`` for a small fixed set), and a **``/provider``** slash
  command built on it: a role toggle (``brain``/``hands``/``both``) -> the provider
  ``SelectDialog`` -> the SHARED model-pick step for the just-chosen provider
  (``_pick_model_step``, also now used by ``/model``), persisted via ``persist_role``.
  Per-role isolation holds (the chosen role is the only one touched); ``both`` runs
  the model step twice (brain then hands), each persisted independently; provider +
  model both persist to ``config.json``. ``/assume`` now shows a short per-level
  description DERIVED from the real dial semantics
  (:func:`~relay.config.assumption_summary`, sourced from
  ``_ASSUMPTION_DIRECTIVES`` so it can't drift), with the current level marked. No
  inline args; no forked logic.
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
    assumption_summary,
    default_config,
    describe_resolution,
    load_env,
    load_models,
    resolve_assumption_level,
    resolve_role_field,
)
from relay.context import DEFAULT_CONTEXT_WINDOW, resolve_context_window
from relay.debug import (
    RunSnapshot,
    build_debug_bundle,
    redact_secrets,
    summarize_run,
)
from relay.providers import (
    DEFAULT_PROVIDER,
    DISCOVERY_LIST,
    DISCOVERY_MANUAL,
    ProviderProfile,
    known_providers,
    list_models,
    resolve_provider,
    validate_model,
)
from relay.secrets import (
    get_key,
    remove_key,
    resolve_key,
    set_key,
)
from relay.store import config_dir, config_path, load_config, save_config
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

__version__ = "0.0.21"

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
    "assumption_summary",
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
    # v0.0.16 -- provider config + secrets (beta-enablement)
    "config_dir",
    "config_path",
    "load_config",
    "save_config",
    "default_config",
    "describe_resolution",
    "resolve_role_field",
    "get_key",
    "set_key",
    "remove_key",
    "resolve_key",
    "list_models",
    "validate_model",
    "DISCOVERY_MANUAL",
    "DISCOVERY_LIST",
    # v0.0.22 -- the /log debug export (redactor + bundle builder)
    "redact_secrets",
    "build_debug_bundle",
    "summarize_run",
    "RunSnapshot",
    "__version__",
]
