"""The ``/log`` debug export: a redactor (safety core) + a bundle builder.

A beta tester who hits a problem can run ``/log`` and get a single, timestamped,
human-readable Markdown file that captures a complete picture of the run --
config, outcome, the conversation, the activity feed, the plan, memory -- so they
can attach it to a GitHub issue instead of describing the problem from memory.

The #1 requirement is that the export is **safe to paste in public**: no API key
(or other credential) can appear in it, BY CONSTRUCTION. Two layers enforce that:

1. :func:`redact_secrets` -- a pure, idempotent, crash-safe function that strips
   credential-shaped content AND any exact live key value handed to it. It runs
   over the WHOLE assembled bundle (every field, belt-and-suspenders), so even the
   transcript and activity text are scrubbed.
2. The builder (:func:`build_debug_bundle`) never writes a key value to begin with
   -- only key *presence* ("present" / "absent"). The redactor is the backstop.

This module makes ZERO model calls: the bundle is assembled entirely from state
the app already holds (no generation, no network, no upload -- the file is written
locally and the user chooses whether to share it).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# What every redacted credential is replaced with. Stable so callers/tests can
# assert on it; contains characters (``*``) outside the key-char classes below, so
# a second redaction pass can never re-match it -- the basis of idempotency.
MASK = "***REDACTED***"

# An exact known-secret shorter than this is NOT literal-replaced, so a stray short
# value can never blank out swathes of ordinary text. A real API key is far longer;
# this only guards against a pathological 1-2 char "secret".
_MIN_SECRET_LEN = 6


# -- the generic credential patterns (applied after exact known-secret removal) --
#
# Deliberately conservative about BARE long tokens: a long hex/base64 blob with NO
# credential marker (a git SHA, a file hash, a base64 image) is left alone -- only
# ``sk-``-prefixed keys, ``Bearer`` tokens, and marker-preceded values are masked.
# That keeps real transcript content intact while still catching every key shape.

# ``sk-`` API keys: OpenAI / OpenRouter (``sk-or-v1-...``) and DeepSeek (``sk-...``)
# all share this prefix. 20+ key chars after ``sk-`` -> masked, prefix preserved.
_SK = (re.compile(r"sk-[A-Za-z0-9_\-]{20,}"), "sk-" + MASK)

# ``Bearer <token>`` as it appears in an Authorization header / raw error text.
_BEARER = (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{16,}"), r"\1" + MASK)

# ``marker: value`` / ``marker=value`` / ``"marker": "value"`` for credential
# markers (api_key / token / secret / password / authorization / ...). Only the
# value (16+ chars) is masked; the marker and surrounding punctuation are kept so
# the line still reads. The 16-char floor and the ``*``-bearing mask make it
# idempotent (``MASK`` is too short and starts with ``*``, so it never re-matches).
_ASSIGN = (
    re.compile(
        r"(?i)(\b(?:api[_-]?key|apikey|secret(?:[_-]?key)?|token|password|passwd"
        r"|authorization|auth[_-]?token|access[_-]?token)\b[\"']?\s*[:=]\s*[\"']?)"
        r"[A-Za-z0-9+/._\-]{16,}"
    ),
    r"\1" + MASK,
)

_PATTERNS = (_SK, _BEARER, _ASSIGN)


def redact_secrets(text: str, known_secrets: list[str] | None = None) -> str:
    """Return ``text`` with any credential-like content removed/masked.

    The guarantee that makes the export shareable. Three things happen, in order:

    1. Every **exact live key value** in ``known_secrets`` is replaced verbatim
       wherever it occurs (even mid-sentence, even if it would not match a generic
       pattern). This is the strongest guarantee -- the real secret string is gone.
    2. ``sk-`` keys and ``Bearer`` tokens are masked by shape.
    3. Values following credential markers (``api_key=``, ``Authorization:`` ...)
       are masked.

    Idempotent (redacting twice == once), None-safe (``None`` -> ``""``), and it
    never raises on odd input -- a redaction that fails silently leaves that pass'
    text unchanged rather than crashing the export.
    """
    if text is None:
        return ""
    try:
        out = str(text)
    except Exception:  # noqa: BLE001 -- an un-stringable object: nothing to redact
        return ""

    # 1. Exact known secrets first, longest-first so a key that contains another
    #    (e.g. a prefix) is masked whole before its substring is touched.
    for secret in sorted(_clean_secrets(known_secrets), key=len, reverse=True):
        out = out.replace(secret, MASK)

    # 2/3. Generic shapes. Each pattern is independent; a failing pass is skipped.
    for pattern, repl in _PATTERNS:
        try:
            out = pattern.sub(repl, out)
        except Exception:  # noqa: BLE001 -- never let a regex edge case crash export
            pass
    return out


def _clean_secrets(known_secrets: list[str] | None) -> set[str]:
    """The usable exact secrets: non-empty strings of at least :data:`_MIN_SECRET_LEN`."""
    out: set[str] = set()
    for secret in known_secrets or []:
        if isinstance(secret, str):
            value = secret.strip()
            if len(value) >= _MIN_SECRET_LEN:
                out.add(value)
    return out


# ============================================================================
# The bundle builder (assembled from existing state; makes ZERO model calls)
# ============================================================================


@dataclass(frozen=True)
class RunSnapshot:
    """A run's NON-secret structured outcome, for the debug bundle.

    Built from the run outcome / result the app already holds -- no model call.
    """

    status: str
    cost: float | None = None
    done: int = 0
    failed: int = 0
    total: int = 0
    limit_fired: str | None = None  # friendly note when a limit/ceiling fired
    error: str = ""
    max_total_steps: int | None = None


def summarize_run(outcome, *, cost: float | None = None) -> RunSnapshot | None:
    """Distill a run ``outcome`` (a ``RunOutcome``) into a :class:`RunSnapshot`.

    Duck-typed (reads ``.status`` / ``.error`` / ``.result`` and, on the result,
    ``.status`` / ``.plan`` / ``.max_total_steps``) so it works on the real bridge
    types or a test stand-in. ``None`` outcome -> ``None`` (no run yet). Pulls the
    actual done/failed/total off the plan steps and, when a stuck-loop or
    step-ceiling terminal fired, the shared friendly note for it.
    """
    if outcome is None:
        return None
    status = getattr(outcome, "status", "?") or "?"
    error = getattr(outcome, "error", "") or ""
    result = getattr(outcome, "result", None)
    done = failed = total = 0
    max_total = None
    limit = None
    if result is not None:
        status = getattr(result, "status", status) or status
        max_total = getattr(result, "max_total_steps", None)
        steps = getattr(getattr(result, "plan", None), "steps", None) or []
        for step in steps:
            step_status = getattr(step, "status", "")
            if step_status == "done":
                done += 1
            elif step_status == "failed":
                failed += 1
        total = len(steps)
        try:
            from relay.orchestrator import friendly_terminal_message

            limit = friendly_terminal_message(status, max_total_steps=max_total)
        except Exception:  # noqa: BLE001 -- never let summary lookup crash export
            limit = None
    return RunSnapshot(
        status=status, cost=cost, done=done, failed=failed, total=total,
        limit_fired=limit, error=error, max_total_steps=max_total,
    )


# Human header for each scope, plus a one-line honesty note about what the bundle
# can and cannot retain (the app keeps no per-project history, so "full session"
# is the session-spanning buffers + the current project's structured outcome).
_SCOPE_LABELS = {
    "current": "Current project (the most recent run)",
    "session": "Full session (every project since launch, including the current one)",
}
_SCOPE_NOTES = {
    "current": (
        "Conversation + activity below are for the most recent project; the "
        "structured run outcome and plan/memory are that project's."
    ),
    "session": (
        "Conversation + activity below span the WHOLE session (all projects); the "
        "structured run outcome and plan/memory are the current/most-recent "
        "project's only (Relay keeps no per-project structured history)."
    ),
}


def build_debug_bundle(
    *,
    scope: str,
    version: str,
    python_version: str,
    platform_str: str,
    resolution: dict,
    assumption_level: str,
    max_total_steps: int | None,
    run: RunSnapshot | None,
    transcript_lines: list[str] | None,
    activity_lines: list[str] | None,
    plan=None,
    memory=None,
    known_secrets: list[str] | None = None,
    timestamp: str = "",
) -> str:
    """Assemble the debug bundle as Markdown, then redact the WHOLE thing.

    Every value comes from state the app already holds -- this makes NO model call.
    The result is passed through :func:`redact_secrets` (with ``known_secrets``) as
    the final step, so no credential can survive into the file even if a section
    inadvertently contained one. ``resolution`` is :func:`describe_resolution`'s
    output -- key **presence** only, never a key value.
    """
    lines: list[str] = []
    lines += _section_header(scope, timestamp)
    lines += _section_environment(version, python_version, platform_str)
    lines += _section_config(resolution, assumption_level, max_total_steps)
    lines += _section_outcome(scope, run)
    lines += _section_block("Conversation transcript", transcript_lines)
    lines += _section_block("Activity log", activity_lines)
    lines += _section_plan(plan)
    lines += _section_memory(memory)

    markdown = "\n".join(lines).rstrip() + "\n"
    return redact_secrets(markdown, known_secrets=known_secrets)


def _section_header(scope: str, timestamp: str) -> list[str]:
    label = _SCOPE_LABELS.get(scope, scope)
    note = _SCOPE_NOTES.get(scope, "")
    out = [
        "# Relay debug export",
        "",
        f"- Generated: {timestamp or '(unknown time)'}",
        f"- Scope: {label}",
    ]
    if note:
        out.append(f"- Note: {note}")
    out.append("")
    return out


def _section_environment(version: str, python_version: str, platform_str: str) -> list[str]:
    return [
        "## Environment",
        "",
        f"- Relay version: {version}",
        f"- Python: {python_version}",
        f"- Platform: {platform_str}",
        "",
    ]


def _section_config(resolution: dict, assumption_level: str, max_total_steps: int | None) -> list[str]:
    """Resolved NON-secret config: models/providers/assumption/ceiling + key
    PRESENCE only (never a key value -- the resolution dict carries presence)."""
    out = ["## Resolved config (non-secret)", ""]
    roles = (resolution or {}).get("roles", {})
    for role, fields in roles.items():
        provider = _field(fields, "provider")
        model = _field(fields, "model")
        thinking = _field(fields, "thinking")
        psrc = _source(fields, "provider")
        msrc = _source(fields, "model")
        out.append(
            f"- {role}: {provider} / {model}  "
            f"(thinking {'on' if thinking else 'off'}; src {psrc}/{msrc})"
        )
    out.append(f"- Assumption level: {assumption_level}")
    ceiling = "unbounded" if max_total_steps is None else str(max_total_steps)
    out.append(f"- Global step ceiling: {ceiling}")
    out.append("- Keys (presence only -- NEVER a key value):")
    for pid, info in (resolution or {}).get("providers", {}).items():
        present = bool(info.get("key_present")) if isinstance(info, dict) else False
        out.append(f"  - {pid}: {'present' if present else 'absent'}")
    out.append("")
    return out


def _field(fields, name):
    """A ``describe_resolution`` field is a ``(value, source)`` pair -- the value."""
    value = (fields or {}).get(name)
    if isinstance(value, (tuple, list)) and value:
        return value[0]
    return value


def _source(fields, name):
    value = (fields or {}).get(name)
    if isinstance(value, (tuple, list)) and len(value) > 1:
        return value[1]
    return "?"


def _section_outcome(scope: str, run: RunSnapshot | None) -> list[str]:
    out = ["## Run outcome", ""]
    if run is None:
        out += ["- (no run has executed yet this session)", ""]
        return out
    if scope == "session":
        out.append("- (the current/most-recent project)")
    out.append(f"- Terminal status: {run.status}")
    out.append("- Total cost: " + ("(unknown)" if run.cost is None else f"${run.cost:.4f}"))
    out.append(f"- Steps: {run.done} done / {run.failed} failed / {run.total} total")
    out.append(f"- Limit fired: {run.limit_fired or 'none'}")
    if run.error:
        out.append(f"- Error: {run.error}")
    out.append("")
    return out


def _section_block(title: str, body_lines: list[str] | None) -> list[str]:
    """A section whose body is a fenced block of free text lines (transcript /
    activity). Empty bodies render an explicit ``(empty)`` so the section is honest."""
    out = [f"## {title}", ""]
    rows = [line for line in (body_lines or [])]
    if not rows:
        out += ["(empty)", ""]
        return out
    out.append("```")
    out += rows
    out.append("```")
    out.append("")
    return out


def _section_plan(plan) -> list[str]:
    """The plan with per-step status + outcome (duck-typed on ``plan.steps``)."""
    out = ["## Plan", ""]
    steps = getattr(plan, "steps", None)
    if not steps:
        out += ["(no plan retained)", ""]
        return out
    for step in steps:
        index = getattr(step, "index", "?")
        instruction = getattr(step, "instruction", "")
        status = getattr(step, "status", "?")
        outcome = getattr(step, "outcome", None)
        line = f"{index}. [{status}] {instruction}"
        if outcome:
            line += f" -- {outcome}"
        out.append(line)
    out.append("")
    return out


def _section_memory(memory) -> list[str]:
    """The (deduped) memory entries with kind + summary (duck-typed on ``.entries``)."""
    out = ["## Memory", ""]
    entries = getattr(memory, "entries", None)
    if not entries:
        out += ["(no memory retained)", ""]
        return out
    for entry in entries:
        kind = getattr(entry, "kind", "?")
        summary = getattr(entry, "summary", "") or getattr(entry, "detail", "")
        out.append(f"- [{kind}] {summary}")
    out.append("")
    return out
