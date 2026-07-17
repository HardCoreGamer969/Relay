"""Pure text/event presentation helpers for the Relay TUI."""

from __future__ import annotations

import re

from relay.config import ModelConfig
from relay.orchestrator import Event
from relay.transcript import Turn

from .theme import ACTOR_BRAIN, ACTOR_HANDS, ACTOR_YOU

def present_prompt(text: str) -> str:
    """THE chokepoint for every user-facing question/prompt string.

    v1 passes full-fidelity text through unchanged. Prompt 2's
    experience-level projection (rephrasing per user expertise) plugs in here
    -- one place, no refactor.
    """
    return text


def format_turn(turn: Turn) -> str:
    """Render one transcript turn for the conversation pane.

    UNICODE-CLEAN by contract: the turn text is passed through verbatim (no
    ASCII sanitizing, no ellipsis truncation) -- Textual renders real unicode
    natively, unlike the legacy Windows console path.
    """
    who = "you" if turn.speaker == "user" else "brain"
    return f"{who} ({turn.phase}): {turn.text}"


def model_identity(models: ModelConfig) -> str:
    """The brain/hands pairing as IDENTITY (welcome screen), not a status note.

    This is the user knowing which pairing they're about to spend money on,
    front and center -- so it reads as the machine's name, cleanly styled.
    """
    return f"brain ~{models.brain}  ·  hands ~{models.hands}"


# -- friendly provider errors (the catch-all so raw API JSON never reaches a user) --

# Pretty provider labels for user-facing error text (fall back to the raw id).
_PROVIDER_LABELS = {"openrouter": "OpenRouter", "deepseek": "DeepSeek"}

# Markers that betray a raw provider/API error blob (JSON / status line) we must
# never surface verbatim.
_RAW_ERROR_MARKERS = ("{'error'", '{"error"', "'raw'", '"raw"', "error code:", "traceback")


def _provider_label(provider: str | None) -> str:
    return _PROVIDER_LABELS.get(provider, provider) if provider else "The provider"


def _is_raw_provider_error(text: str) -> bool:
    """Whether ``text`` looks like a raw provider/API error blob (don't show it raw)."""
    low = text.lower()
    return any(marker in low for marker in _RAW_ERROR_MARKERS)


def _http_status(text: str) -> str | None:
    """Pull an HTTP-ish 4xx/5xx status code out of a provider error string."""
    match = re.search(r"\b([45]\d\d)\b", text)
    return match.group(1) if match else None


def friendly_provider_error(error, *, provider: str | None = None, model: str | None = None) -> str:
    """Render a raw provider/API error as a friendly, ASCII-safe one-liner.

    THE catch-all net: at every point a provider error would reach the UI (the
    run-error path and the slash live calls -- validation, listing, doctor), this
    states what failed, which provider/model, and a short hint to re-pick -- and
    NEVER includes the raw ``{'error': {... 'raw': ...}}`` payload (which may be
    logged at debug elsewhere, but not shown). Text that does NOT look like a raw
    provider error is returned unchanged, so a clean validation note ("'x' is not in
    deepseek's live model list") and a plain non-provider error read normally.
    """
    text = str(error or "").strip()
    if not _is_raw_provider_error(text):
        return text
    label = _provider_label(provider)
    code = _http_status(text)
    code_note = f" (HTTP {code})" if code else ""
    if model:
        lead = (
            f"{label} rejected the request -- '{model}' may not be a valid {label} model"
            if code == "400"
            else f"{label} returned an error{code_note} for '{model}'"
        )
        return f"{lead}. Use /model or /provider to pick a valid one."
    return (
        f"{label} returned an error{code_note}. The model or provider may be invalid -- "
        "check with /doctor, or re-pick via /model or /provider."
    )


def describe_event_for_activity(event: Event) -> tuple[str | None, str]:
    """Map one engine event to ``(actor, line)`` for the activity feed.

    ``actor`` is ``brain`` / ``hands`` / ``you`` (or ``None`` for a system line).
    Every field read here is already present on the emitted event -- nothing is
    fetched, narrated, or summarized by a model.
    """
    kind = event.kind
    p = event.payload or {}
    msg = event.message

    if kind == "step_start":
        return ACTOR_BRAIN, f"-> step {p.get('index')}: {p.get('instruction', msg)}"
    if kind == "exec_action":
        return ACTOR_HANDS, msg  # describe_action text; observation appended by caller
    if kind == "exec_parse_failure":
        return ACTOR_HANDS, f"! parse failure: {p.get('snippet', '')}"
    if kind == "executor_question":
        return ACTOR_HANDS, f"? {p.get('question', msg)}"
    if kind == "brain_self_answered":
        return ACTOR_BRAIN, f"answers: {p.get('answer', '')}"
    if kind == "brain_escalated":
        return ACTOR_BRAIN, f"escalates: {p.get('question', msg)}"
    if kind == "user_decided":
        return ACTOR_YOU, f"decided: {p.get('answer', msg)}"
    if kind == "step_reviewed":
        return ACTOR_BRAIN, f"reviews step {p.get('index')}: {p.get('verdict', '')}"
    if kind == "step_done":
        return ACTOR_HANDS, f"done step {p.get('index')}: {p.get('outcome', '')}"
    if kind == "step_failed":
        return ACTOR_HANDS, f"failed step {p.get('index')}: {p.get('reason', '')}"
    if kind == "plan_created":
        return ACTOR_BRAIN, f"plan: {len(p.get('steps') or [])} step(s)"
    if kind == "plan_proposed":
        return ACTOR_BRAIN, f"proposed a plan ({len(p.get('steps') or [])} step(s))"
    if kind in ("plan_revised", "replanned"):
        return ACTOR_BRAIN, f"revised the plan ({len(p.get('steps') or [])} step(s))"
    if kind == "escalation":
        return ACTOR_BRAIN, msg
    if kind == "envelope_warn":
        return None, f"! {msg}"
    if kind == "memory_write":
        return ACTOR_BRAIN, f"memory += [{p.get('kind', '')}] {p.get('summary', '')}"
    if kind == "scope_assessed":
        return ACTOR_BRAIN, f"scope: {p.get('scope', '')} -> {p.get('posture', '')}"
    if kind in ("scoping_question", "elicitation", "clarify"):
        return ACTOR_BRAIN, f"asks: {p.get('question', msg)}"
    if kind == "user_reacted":
        return ACTOR_YOU, f"reacted: {p.get('reaction', msg)}"
    if kind == "rejected":
        return ACTOR_YOU, "rejected the plan"
    if kind == "committed":
        return ACTOR_YOU, "committed the plan"
    # status / transcript_compacted / not_committed / anything else: a system line.
    return None, msg
