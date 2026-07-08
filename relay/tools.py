"""The executor's hands: the tools the agent loop can invoke.

Each filesystem tool resolves paths relative to a ``project_root`` and refuses
to touch anything outside it (resolve-then-check; see ``_resolve``).

``bash`` additionally consults the command policy (``relay.policy``) before
running anything: it refuses ``BLOCKED`` commands outright and gates
``CONFIRM`` commands behind an approver callback. NOTE the honest limit --
the policy is a best-effort speed bump against obvious destructive commands, NOT a
security sandbox. cwd-pinning + string classification do not contain an
adversarial command (env expansion, command substitution, eval, etc. evade
both). Real isolation (process/container sandboxing) is a later milestone (v0.95).

v0.0.32: bash inherits a SECRET-SCRUBBED environment (every key/token/secret
env var is dropped before exec) and every observation is run through
:func:`relay.debug.redact_secrets` before it reaches the model. This closes
the exfiltration path where a model could ``env`` / ``set`` / read a ``.env``
file and observe the parent's API keys, then send them back as part of its
own next message.
"""

from __future__ import annotations

import hashlib
import html
import os
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from relay.debug import redact_secrets
from relay.policy import BLOCKED, CONFIRM, classify


class ToolError(Exception):
    """A tool could not complete (missing file, bad path, etc.)."""


class PathEscapeError(ToolError):
    """A path resolved outside ``project_root`` and was refused."""


class PatchError(ToolError):
    """An ``apply_patch`` envelope was malformed or a section failed validation.

    The message names the failing part so the (recoverable) observation tells the
    hands exactly what to fix -- consistent with the loop's other refusals.
    """


# Cap on the number of paths a single `glob` returns, so a broad pattern (e.g.
# ``**/*``) can't flood the transcript / reviewer context. Truncation is noted.
GLOB_MATCH_CAP = 200

# webfetch bounds (v0.0.26): the one network-touching tool. Bounded length + a real
# timeout so it can't flood context or hang the loop; a real User-Agent because the
# default urllib UA is 403'd by some hosts (e.g. models.dev). stdlib only -- no dep.
WEBFETCH_CHAR_CAP = 4000
WEBFETCH_MAX_BYTES = 2_000_000
WEBFETCH_TIMEOUT_S = 15

# v0.0.32: text observations (read, grep, bash) are bounded the same way glob
# already was. A 50k-line read or a verbose build log would otherwise balloon
# every downstream call's context and turn one careless tool call into a silent
# money-leak. The cap is per observation: below it, the text is returned as-is;
# above it, the head and tail are kept and a "(N lines truncated)" marker
# makes the omission honest. The agent can re-``read`` with a narrower scope
# (``head``-style patterns or smaller paths) if it needs the middle.
OBSERVATION_LINE_CAP = 200
OBSERVATION_LINE_HEAD = 100  # when truncated, keep this many from the top
OBSERVATION_LINE_TAIL = 100  # ... and this many from the bottom
OBSERVATION_CHAR_CAP = 50_000  # hard ceiling on any one observation's chars

# v0.0.32: bash inherits a SECRET-SCRUBBED env. Any env var whose name matches
# one of these suffix patterns is dropped before exec -- so a model that runs
# ``env`` or ``set`` can't see ``OPENROUTER_API_KEY`` (or any of the other
# shell-injected credentials the parent process carries), and even if a key
# leaks through some other path the post-run ``redact_secrets`` pass would
# still mask it in the observation. Conservative on purpose: false positives
# (a non-credential ``FOO_TOKEN``) only cost the user a missing env var;
# a false negative (a secret leaked to the model) costs them a key.
_SECRET_ENV_SUFFIXES = (
    "_API_KEY", "_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_PASSWD",
    "_AUTH", "_CREDENTIAL", "_CREDENTIALS", "_PRIVATE_KEY",
)
_WEBFETCH_UA = "Relay/0.0.26 (coding agent; +https://github.com/)"

_SCRIPT_STYLE_RE = re.compile(r"(?is)<(script|style)\b.*?</\1>")
_TAG_RE = re.compile(r"(?s)<[^>]+>")


def _http_get(url: str) -> str:
    """GET ``url`` and return the decoded body text (stdlib urllib).

    A thin seam so tests can monkeypatch it (``relay.tools._http_get``) and stay
    network-free. Sets a real User-Agent, a timeout, and a byte cap.
    """
    request = urllib.request.Request(url, headers={"User-Agent": _WEBFETCH_UA})
    with urllib.request.urlopen(request, timeout=WEBFETCH_TIMEOUT_S) as response:
        raw = response.read(WEBFETCH_MAX_BYTES)
        charset = response.headers.get_content_charset() or "utf-8"
    return raw.decode(charset, errors="replace")


def _html_to_text(source: str) -> str:
    """Minimal, stdlib HTML -> readable text: drop script/style, strip tags, unescape
    entities, and collapse runaway whitespace. Best-effort (not a full renderer)."""
    text = _SCRIPT_STYLE_RE.sub(" ", source)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n[ \t]*\n\s*", "\n\n", text)
    return text.strip()


def _short_fetch_reason(exc: Exception) -> str:
    """A concise, ASCII-safe reason for a failed fetch (never a traceback/blob)."""
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return f"{exc.reason}"
    reason = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
    return reason[:120]


def _existing_file_metrics(target: Path) -> tuple[int, int, str] | None:
    """``(byte size, line count, eol)`` of ``target`` if it is an existing regular file,
    else ``None`` (a NEW file -- nothing is being replaced). Read BEFORE an overwrite so
    the observation can reveal a whole-file replacement (v0.0.31) AND the next write can
    preserve the file's native EOL (CRLF vs LF) -- a CRLF file stays CRLF, an LF file
    stays LF, a brand-new file defaults to LF. Never raises."""
    try:
        if not target.is_file():
            return None
        raw = target.read_bytes()
    except OSError:
        return None
    text = raw.decode("utf-8", errors="replace")
    lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    # Detect the file's native EOL: any CRLF in the bytes -> CRLF, else LF. This is the
    # value we'll re-apply on write so we don't silently rewrite the file's line endings
    # on every edit (the v0.0.31 bug: ``read_text(..., newline=None)`` universal-newlines
    # normalized CRLF -> LF before ``has_crlf`` could see it, so writes always went
    # out as LF -- corrupting CRLF files into LF on POSIX and LF files into CRLF on
    # Windows). Using raw bytes here is the fix: the on-disk EOL is visible at the
    # byte level no matter what the text-mode decoding does.
    eol = "\r\n" if b"\r\n" in raw else "\n"
    return len(raw), lines, eol


# --- v0.0.32: byte-exact I/O with EOL preservation ---------------------------
#
# A long-standing bug: read_text/write_text with default ``newline=None`` ran every
# read and write through universal-newlines, which silently translated CRLF <-> LF
# at the stdlib boundary. The fix is to do the text<->bytes conversion explicitly so:
#   1. ``read`` returns text with the file's native EOL preserved (so ``has_crlf`` in
#      apply_patch can see CRLF and the comparison is byte-correct).
#   2. ``write`` / ``edit`` / ``apply_patch`` write bytes in the file's native EOL
#      (a CRLF file stays CRLF, an LF file stays LF; new files default to LF).
# These helpers centralize the conversion so the bug can't regress in a single spot
# while the read/write sites all change.

DEFAULT_EOL = "\n"  # a brand-new file defaults to LF (the universal default)


def _read_text_preserving_eol(path: Path) -> str:
    """Read ``path`` as bytes, decode as UTF-8, return text with native EOL preserved.

    Uses raw bytes so CRLF survives the round-trip (universal-newlines would
    silently translate it to LF). ``errors="replace"`` is the same policy the
    bash subprocess uses: a stray non-UTF-8 byte becomes U+FFFD, never a crash.
    """
    raw = path.read_bytes()
    return raw.decode("utf-8", errors="replace")


def _write_text_preserving_eol(path: Path, text: str, *, eol: str) -> int:
    """Write ``text`` to ``path`` in the given EOL (``"\\n"`` or ``"\\r\\n"``).

    Conversion: if ``eol`` is CRLF, every ``\\n`` in ``text`` becomes ``\\r\\n`` first.
    (Text never has bare ``\\r`` here -- the model emits LF, and our reads preserve the
    file's EOL but the source ``text`` we pass in is always LF-only on the write path.)
    Returns the number of bytes actually written -- what was committed to disk, not the
    character count -- so the caller can report true on-disk size in the observation.
    """
    if eol == "\r\n":
        payload = text.replace("\n", "\r\n").encode("utf-8")
    else:
        payload = text.replace("\r\n", "\n").encode("utf-8")
    path.write_bytes(payload)
    return len(payload)


def _scrubbed_env() -> dict[str, str]:
    """A copy of ``os.environ`` with every secret-shaped var dropped.

    The bash subprocess inherits THIS, not the live process env, so a model
    that runs ``env`` / ``set`` / ``printenv`` / ``cat ~/.bashrc`` can't see
    ``OPENROUTER_API_KEY`` (or any other credential-shaped value the parent
    process happens to carry). The path-based / home-directory tricks are
    out of scope of this scrub -- bash is still a shell -- but the in-process
    leak the plan called out (the parent env) is closed here.

    Anything matching :data:`_SECRET_ENV_SUFFIXES` (case-insensitive) is
    dropped. ``PATH``, ``HOME``, ``LANG``, etc. are kept so the subprocess
    can actually find tools. A new value is returned (the live env is
    untouched) so subsequent calls don't see a permanently mutated state.
    """
    return {
        name: value
        for name, value in os.environ.items()
        if not any(name.upper().endswith(suffix) for suffix in _SECRET_ENV_SUFFIXES)
    }


def _scrubbed_secrets_from_env() -> list[str]:
    """The values of the secret-shaped env vars that exist RIGHT NOW.

    These are passed as ``known_secrets`` to :func:`redact_secrets` so the
    post-run observation is masked even if a command like ``echo $OPENAI_API_KEY``
    or ``python -c "import os; print(os.environ)"`` somehow produced the value
    in the output (a 2nd-line defense after the env-scrub; the env-scrub is
    the primary, this catches the accidental ``export`` or the script that
    re-imports the value).
    """
    secrets: list[str] = []
    for name, value in os.environ.items():
        if any(name.upper().endswith(suffix) for suffix in _SECRET_ENV_SUFFIXES):
            if value and len(value) >= 6:  # same floor as the redactor
                secrets.append(value)
    return secrets


def _redact_observation(text: str, *, extra_secrets: list[str] | None = None) -> str:
    """Run an observation through :func:`redact_secrets` before it reaches the model.

    Every text observation (read / grep / bash / webfetch) goes through this
    so a file that contains a key in plaintext, a stderr line that echoes
    a curl header, or a script that prints an env var, never reaches the
    next model call. Non-text observations (list / glob / mkdir / wrote)
    don't contain user data and skip this step.

    ``extra_secrets`` is for the live env values the parent process is
    carrying, which the redactor would otherwise only catch by pattern
    (sk-/Bearer/marker); the explicit list is a strict superset.
    """
    if not text:
        return text
    return redact_secrets(text, known_secrets=extra_secrets)


def _kill_process_tree(proc: "subprocess.Popen[bytes]") -> None:
    """Kill the bash subprocess AND every descendant, on timeout.

    The bash call uses ``start_new_session=True`` (POSIX new session, Windows
    ``CREATE_NEW_PROCESS_GROUP``) so the subprocess is the root of its own
    process group; killing the group walks every descendant the shell spawned:

    - POSIX: ``os.killpg(proc.pid, SIGTERM)`` walks the group, with a SIGKILL
      escalation if the tree doesn't exit within ~2s.
    - Windows: ``taskkill /T /F /PID <pid>`` ships with every Windows install
      and walks the tree by force. A plain ``proc.kill()`` is the fallback
      when taskkill is unavailable.

    Failures are swallowed -- this is a best-effort cleanup. The timeout has
    already fired and the caller is about to return an observation; orphaning
    one extra process is the lesser evil vs. hanging the worker.
    """
    try:
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                    capture_output=True, timeout=5,
                )
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                try:
                    proc.kill()
                except OSError:
                    pass
        else:
            import signal
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
    except Exception:  # noqa: BLE001 -- cleanup must never mask the TimeoutExpired
        pass


def _cap_observation(text: str) -> str:
    """Bound a tool observation so it can't blow up the model's context window.

    Two independent caps, the larger of which wins:
      - by lines: if ``text`` has more than :data:`OBSERVATION_LINE_CAP` lines,
        keep the first :data:`OBSERVATION_LINE_HEAD` and last
        :data:`OBSERVATION_LINE_TAIL` with an explicit "(N lines truncated)"
        marker between them -- the agent can tell something is missing and
        re-read with a narrower scope if it needs the middle.
      - by chars: a hard ceiling (:data:`OBSERVATION_CHAR_CAP`) on the returned
        text. Lines-cap catches the "50k lines of 1 char each" case; char-cap
        catches the "one 50k-char line" case (e.g. a minified bundle).
    The original text is left alone -- a small text is returned unchanged, so
    the common path is a no-op.
    """
    if not text:
        return text
    # Char cap first: a single huge line is the cheaper case to catch.
    if len(text) > OBSERVATION_CHAR_CAP:
        text = text[:OBSERVATION_CHAR_CAP] + (
            f"\n... (truncated to {OBSERVATION_CHAR_CAP} chars; "
            "narrow the call if you need the rest)"
        )
    # Line cap: keep head + tail so a 50k-line log is still actionable.
    lines = text.splitlines()
    if len(lines) > OBSERVATION_LINE_CAP:
        head = lines[:OBSERVATION_LINE_HEAD]
        tail = lines[-OBSERVATION_LINE_TAIL:]
        omitted = len(lines) - OBSERVATION_LINE_HEAD - OBSERVATION_LINE_TAIL
        marker = f"... ({omitted} lines truncated) ..."
        return "\n".join(head + [marker] + tail)
    return text


def _wrote_observation(
    path: str,
    bytes_written: int,
    *,
    content: str,
    before: tuple[int, int, str] | None = None,
) -> str:
    """The shared ``wrote <path> (<bytes> bytes, <lines> lines)`` line for edit/write.

    ``bytes_written`` is the on-disk byte count AFTER EOL conversion (a 100-char text
    of all-LF content with no ``\r`` writes 100 bytes; the same text on a CRLF target
    writes 200 bytes). Reporting the real on-disk size is what makes the observation
    match ``wc -c`` and what lets the v0.0.31 "replaced entire file" note compare
    apples-to-apples. ``content`` is used ONLY for the line count (which is EOL-agnostic
    -- N LFs == N CRLFs == N lines). ``before`` carries the prior ``(bytes, lines, eol)``
    when an EXISTING file was overwritten -- edit/write replace the WHOLE file, so the
    observation says it replaced the file and by how much. A new file (``before is None``)
    keeps the plain observation -- nothing was destroyed.
    """
    lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
    base = f"wrote {path} ({bytes_written} bytes, {lines} lines)"
    if before is not None:
        before_bytes, before_lines, _eol = before
        base += f" -- replaced entire file (was {before_bytes} bytes, {before_lines} lines)"
    return base


# --- apply_patch: OpenCode's exact envelope (v0.0.26) ------------------------
#
# The envelope is verbatim OpenCode:
#
#   *** Begin Patch
#   *** Add File: <path>          (then one or more "+" lines = initial contents)
#   *** Delete File: <path>       (nothing follows)
#   *** Update File: <path>       (optionally "*** Move to: <newpath>", then @@ hunks)
#   @@ <context>                  (anchor; " "/"-"/"+"  context/remove/add lines)
#   *** End Patch
#
# A patch is parsed into PatchSections, validated WHOLE, then applied all-or-nothing
# (no partial application -- see Tools.apply_patch).

_BEGIN_PATCH = "*** Begin Patch"
_END_PATCH = "*** End Patch"
_ADD = "*** Add File: "
_DELETE = "*** Delete File: "
_UPDATE = "*** Update File: "
_MOVE = "*** Move to: "


@dataclass
class _Hunk:
    """One ``@@``-anchored change within an Update section."""

    anchor: str            # text after "@@ " (an existing line used to locate the hunk)
    before: list[str]      # context + removed lines (what must currently be present)
    after: list[str]       # context + added lines (what replaces it)


@dataclass
class PatchSection:
    """One file operation inside an apply_patch envelope."""

    op: str                # "add" | "update" | "delete"
    path: str
    move_to: str | None = None
    add_lines: list[str] = field(default_factory=list)  # for op == "add"
    hunks: list[_Hunk] = field(default_factory=list)     # for op == "update"


def parse_patch(text: str) -> list[PatchSection]:
    """Parse an OpenCode patch envelope into ordered :class:`PatchSection`s.

    Raises :class:`PatchError` (naming the failing part) on any malformed envelope:
    a missing Begin/End line, an unknown header, an Add line not starting with ``+``,
    an Update body without ``@@`` hunks, or a bad change line.
    """
    lines = (text or "").splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines or lines[0].strip() != _BEGIN_PATCH:
        raise PatchError('patch must start with "*** Begin Patch"')
    if lines[-1].strip() != _END_PATCH:
        raise PatchError('patch must end with "*** End Patch"')

    body = lines[1:-1]
    sections: list[PatchSection] = []
    i, n = 0, len(body)
    while i < n:
        line = body[i]
        if not line.strip():
            i += 1
            continue
        if line.startswith(_ADD):
            path = line[len(_ADD):].strip()
            i += 1
            add_lines: list[str] = []
            while i < n and not body[i].startswith("*** "):
                if not body[i].startswith("+"):
                    raise PatchError(f'Add File "{path}": every line must start with "+"')
                add_lines.append(body[i][1:])
                i += 1
            sections.append(PatchSection(op="add", path=path, add_lines=add_lines))
        elif line.startswith(_DELETE):
            sections.append(PatchSection(op="delete", path=line[len(_DELETE):].strip()))
            i += 1
        elif line.startswith(_UPDATE):
            path = line[len(_UPDATE):].strip()
            i += 1
            move_to = None
            if i < n and body[i].startswith(_MOVE):
                move_to = body[i][len(_MOVE):].strip()
                i += 1
            hunks: list[_Hunk] = []
            while i < n and not body[i].startswith("*** "):
                if not body[i].startswith("@@"):
                    raise PatchError(
                        f'Update File "{path}": expected an "@@" hunk header, got {body[i]!r}'
                    )
                anchor = body[i][2:].strip()
                i += 1
                before: list[str] = []
                after: list[str] = []
                while i < n and not body[i].startswith("*** ") and not body[i].startswith("@@"):
                    change = body[i]
                    if change.startswith("+"):
                        after.append(change[1:])
                    elif change.startswith("-"):
                        before.append(change[1:])
                    elif change.startswith(" "):
                        before.append(change[1:])
                        after.append(change[1:])
                    elif change == "":
                        before.append("")
                        after.append("")
                    else:
                        raise PatchError(
                            f'Update File "{path}": change lines must start with " ", "+", or "-"; '
                            f"got {change!r}"
                        )
                    i += 1
                hunks.append(_Hunk(anchor=anchor, before=before, after=after))
            if not hunks:
                raise PatchError(f'Update File "{path}": no "@@" hunks')
            sections.append(PatchSection(op="update", path=path, move_to=move_to, hunks=hunks))
        else:
            raise PatchError(f"unknown patch line: {line!r}")

    if not sections:
        raise PatchError("patch contains no file sections")
    return sections


# --- fuzzy hunk matching (v0.0.27): OpenCode's progressive-leniency cascade ---
#
# The hands emits patches whose context near-misses the file (smart quotes,
# em-dashes, non-breaking spaces, trailing/leading whitespace) -- exact-only
# matching failed on the FIRST real apply_patch use. We locate both the ``@@``
# anchor and the old-lines block via a four-pass cascade (mirroring OpenCode's
# ``seekSequence`` / ``tryMatch``): each pass tries the WHOLE pattern with a more
# lenient comparator, and the first pass that locates it anywhere wins. This is
# per-pass whole-pattern equality (never partial/substring), so it cannot match a
# wrong location -- a genuine content mismatch still fails (and the patch aborts).
#
# NOTE: OpenCode also has an end-of-file anchor (``*** End of File``) that searches
# from the file end first. Relay's envelope has no such marker (an anchor is just the
# ``@@`` text), so that leniency does not apply here and is intentionally skipped.

# Common Unicode punctuation -> ASCII, applied (after trimming) only in the final,
# most-lenient pass. ``…`` -> ``...`` is multi-char, so we sub via regex (not a
# 1:1 str.translate).
_PUNCT_MAP = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",   # single quotes
    "“": '"', "”": '"', "„": '"', "‟": '"',   # double quotes
    "‐": "-", "‑": "-", "‒": "-",                   # dashes
    "–": "-", "—": "-", "―": "-",
    "…": "...",                                               # ellipsis
    " ": " ",                                                 # non-breaking space
}
_PUNCT_RE = re.compile("|".join(re.escape(ch) for ch in _PUNCT_MAP))


def _normalize_unicode(text: str) -> str:
    """Map common Unicode punctuation (smart quotes, dashes, ellipsis, NBSP) to ASCII."""
    return _PUNCT_RE.sub(lambda m: _PUNCT_MAP[m.group(0)], text)


# The four passes, exact -> most lenient. Each is a whole-line comparator.
def _cmp_exact(a: str, b: str) -> bool:
    return a == b


def _cmp_rstrip(a: str, b: str) -> bool:
    return a.rstrip() == b.rstrip()


def _cmp_trim(a: str, b: str) -> bool:
    return a.strip() == b.strip()


def _cmp_normalized(a: str, b: str) -> bool:
    return _normalize_unicode(a.strip()) == _normalize_unicode(b.strip())


_COMPARATORS = (_cmp_exact, _cmp_rstrip, _cmp_trim, _cmp_normalized)


def _try_match(lines: list[str], pattern: list[str], start: int, compare) -> bool:
    """Whether ``pattern`` matches ``lines[start:start+len]`` line-for-line under
    ``compare`` (the whole pattern, not a partial/substring match)."""
    if start < 0 or start + len(pattern) > len(lines):
        return False
    return all(compare(lines[start + k], pattern[k]) for k in range(len(pattern)))


def _seek_once(lines: list[str], pattern: list[str], start: int) -> int:
    """Locate ``pattern`` at/after ``start`` via the four-pass cascade. ``-1`` if no
    pass matches anywhere. An empty pattern matches at ``start`` (degenerate)."""
    if not pattern:
        return start
    for compare in _COMPARATORS:
        for i in range(start, len(lines) - len(pattern) + 1):
            if _try_match(lines, pattern, i, compare):
                return i
    return -1


def _seek_sequence(lines: list[str], pattern: list[str], start: int = 0) -> int:
    """:func:`_seek_once` plus the trailing-empty-line retry: if the pattern does not
    locate and its last line is empty, drop that trailing empty line and retry."""
    idx = _seek_once(lines, pattern, start)
    if idx == -1 and pattern and pattern[-1] == "":
        idx = _seek_once(lines, pattern[:-1], start)
    return idx


def _apply_hunks(original: str, hunks: list[_Hunk], path: str) -> str:
    """Apply ``hunks`` to ``original`` text, returning the new text.

    Raises :class:`PatchError` if an anchor or a hunk's context block does not
    locate -- the caller relies on this for atomic validation (a non-locating hunk
    fails the WHOLE patch before anything is written).

    Line-ending preservation: the file's original line ending style (CRLF vs LF) is
    detected and re-applied to the result. The matching/work lines are ``\\r``-stripped
    so the fuzzy comparators (which match the model's CRLF-free hunk lines) work, but
    the output preserves the file's native style -- a CRLF file stays CRLF, an LF file
    stays LF. Without this, a "no-op" patch on a CRLF file would silently normalize to
    LF (v0.0.31 fix)."""
    has_crlf = "\r\n" in original
    trailing_nl = original.endswith("\n")
    # Split on \n, then strip \r from each line for matching (the comparators are
    # \r-agnostic via _cmp_rstrip/_cmp_trim, but the replacement lines from the model
    # have no \r, so we must work in a \r-free space and re-apply \r\n at the end).
    raw_lines = original.split("\n")
    if trailing_nl:
        raw_lines = raw_lines[:-1]  # drop the empty tail from the trailing newline
    lines = [line.rstrip("\r") for line in raw_lines]
    for hunk in hunks:
        start = 0
        anchored = bool(hunk.anchor)
        if anchored:
            idx = _seek_sequence(lines, [hunk.anchor], 0)
            if idx == -1:
                raise PatchError(f'Update File "{path}": anchor not found: "@@ {hunk.anchor}"')
            start = idx  # search INCLUSIVE of the anchor: it may be the first changed line
        if hunk.before:
            pos = _seek_sequence(lines, hunk.before, start)
            if pos == -1:
                raise PatchError(
                    f'Update File "{path}": a hunk\'s context did not match the file'
                )
            lines[pos:pos + len(hunk.before)] = hunk.after
        else:  # pure insertion: after the anchor line, or at the top when unanchored
            at = start + 1 if anchored else 0
            lines[at:at] = hunk.after
    result = "\n".join(lines)
    if trailing_nl:
        result += "\n"
    # Re-apply the original line ending style so the file stays in its native format.
    if has_crlf:
        result = result.replace("\n", "\r\n")
    return result


@dataclass
class Tools:
    """Filesystem + shell tools confined to ``project_root``.

    ``approver`` decides ``CONFIRM``-category bash commands: it receives
    ``(command, reason)`` and returns True to run. When no approver is given and
    ``auto_approve`` is False, ``CONFIRM`` commands are denied (the safe default
    for non-interactive contexts). ``auto_approve`` approves ``CONFIRM`` commands
    without asking -- but never affects ``BLOCKED`` commands, which are always
    refused.

    ``bash_timeout_s`` bounds how long a bash command may run (default 120s). A
    hung command (a server, a REPL, ``tail -f``) would otherwise block the worker
    thread forever -- ``cancel_check`` is polled only before the next model call,
    not during a subprocess. ``None`` disables the timeout (unbounded).
    """

    project_root: Path
    approver: Callable[[str, str], bool] | None = None
    auto_approve: bool = False
    bash_timeout_s: float | None = 120.0

    def __post_init__(self) -> None:
        self.project_root = Path(self.project_root)

    def _resolve(self, path: str) -> Path:
        """Resolve ``path`` and refuse anything that escapes the project root.

        Resolve-then-check: both the candidate and the root are resolved to
        absolute real paths *first* -- ``Path.resolve()`` collapses ``..`` AND
        follows symlinks -- and only then do we verify the candidate is inside
        the root. Resolving before checking is what closes the symlink hole: a
        symlink that sits inside the root but points outside it resolves to its
        real (outside) target and is refused, whereas a raw-string check for
        ``..`` would let it through and then read/edit/bash outside the root.

        An ``OSError`` during resolve (network mount, broken symlink, permission)
        is caught and surfaced as a :class:`ToolError` so the loop's existing
        error handling feeds it back to the model as a recoverable observation.
        """
        try:
            root = self.project_root.resolve()
            target = (root / path).resolve()
        except OSError as exc:
            raise ToolError(f"cannot resolve path {path!r}: {exc}") from exc
        if not target.is_relative_to(root):
            raise PathEscapeError(
                f"path {path!r} resolves outside the project root and was refused"
            )
        return target

    def read(self, path: str) -> str:
        """Return the contents of a file, with its native EOL preserved.

        Reads as bytes and decodes as UTF-8 (never through universal-newlines), so
        CRLF stays CRLF -- the apply_patch CRLF-preservation logic and the agentic
        reviewer's content hash both depend on this. The result is then capped
        (head + tail + ``(N lines truncated)`` marker) so a large file can't
        blow up the model's context window; the agent can re-read with a narrower
        scope if it needs the middle. Finally, secrets present in the parent's
        process env are masked (so a ``read`` of ``.env`` can't leak the user's
        API keys back into the model's next message).
        """
        target = self._resolve(path)
        if not target.exists():
            raise ToolError(f"no such file: {path}")
        if target.is_dir():
            raise ToolError(f"{path} is a directory, not a file")
        return _cap_observation(
            _redact_observation(_read_text_preserving_eol(target),
                                extra_secrets=_scrubbed_secrets_from_env())
        )

    def list(self, path: str = ".") -> str:
        """Return a directory listing (directories suffixed with ``/``)."""
        target = self._resolve(path)
        if not target.exists():
            raise ToolError(f"no such path: {path}")
        if target.is_file():
            return target.name
        entries = sorted(
            child.name + ("/" if child.is_dir() else "") for child in target.iterdir()
        )
        return "\n".join(entries) if entries else "(empty directory)"

    def grep(self, pattern: str, path: str) -> str:
        """Return lines matching ``pattern`` with line numbers.

        ``path`` may be a file or a directory (searched recursively). When more
        than one file matches, lines are prefixed with the file's relative path.
        """
        target = self._resolve(path)
        if not target.exists():
            raise ToolError(f"no such path: {path}")
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            raise ToolError(f"invalid regex {pattern!r}: {exc}")

        root = self.project_root.resolve()
        files = (
            [target]
            if target.is_file()
            else [p for p in sorted(target.rglob("*")) if p.is_file()]
        )
        multi = len(files) > 1
        out: list[str] = []
        for f in files:
            try:
                text = _read_text_preserving_eol(f)
            except (UnicodeDecodeError, OSError):
                continue  # skip binary / unreadable files
            prefix = f"{f.relative_to(root).as_posix()}:" if multi else ""
            for lineno, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    out.append(f"{prefix}{lineno}: {line}")
        # The whole-grep result is also a tool observation: cap it the same way
        # read is, so a recursive grep across node_modules can't flood the loop.
        # Redact too -- a grep that hits a .env line would otherwise echo the
        # key value back to the model.
        return _cap_observation(
            _redact_observation(
                "\n".join(out) if out else "(no matches)",
                extra_secrets=_scrubbed_secrets_from_env(),
            )
        )

    def glob(self, pattern: str, base: str = ".") -> str:
        """Return paths matching ``pattern`` under ``base``, relative to the root.

        Uses stdlib :meth:`pathlib.Path.glob` (so ``**`` recurses), one path per line,
        sorted, bounded to :data:`GLOB_MATCH_CAP` (with a truncation note). Read-only --
        no read-guard interaction. ``base`` is confined to the project root like every
        other path; an empty result is a clean ``(no matches)``.
        """
        if not pattern:
            raise ToolError("glob requires a pattern")
        root = self._resolve(base)
        if not root.exists():
            raise ToolError(f"no such path: {base}")
        if not root.is_dir():
            raise ToolError(f"{base} is not a directory")
        project = self.project_root.resolve()
        rels: list[str] = []
        for match in sorted(root.glob(pattern)):
            try:
                rels.append(match.relative_to(project).as_posix())
            except ValueError:  # outside the root (shouldn't happen): show absolute
                rels.append(match.as_posix())
        if not rels:
            return "(no matches)"
        if len(rels) > GLOB_MATCH_CAP:
            shown = rels[:GLOB_MATCH_CAP]
            return "\n".join(shown) + f"\n... ({len(rels) - GLOB_MATCH_CAP} more, truncated)"
        return "\n".join(rels)

    def edit(self, path: str, content: str) -> str:
        """Write ``content`` as the full new contents of ``path``.

        Parent directories are created as needed. (v0.02 is full-file write;
        diff-based edits come in a later milestone.) Preserves the file's native
        EOL on overwrite (a CRLF file stays CRLF, an LF file stays LF); a NEW file
        defaults to LF. The observation reports the real on-disk byte count.
        """
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        before = _existing_file_metrics(target)  # measured BEFORE the overwrite
        eol = before[2] if before is not None else DEFAULT_EOL
        bytes_written = _write_text_preserving_eol(target, content, eol=eol)
        return _wrote_observation(path, bytes_written, content=content, before=before)

    def write(self, path: str, content: str) -> str:
        """Write ``content`` as the WHOLE contents of ``path`` (create or overwrite).

        Distinct from ``edit`` in intent -- ``write`` is the create-or-replace-an-
        entire-file tool. Parent directories are created as needed. The read-before
        -edit guard (in :mod:`relay.loop`) exempts creating a NEW file but requires a
        current read before overwriting an EXISTING one (you shouldn't blind-clobber a
        file you haven't seen); a successful write invalidates the recorded read.
        Preserves the file's native EOL on overwrite; new files default to LF.
        """
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        before = _existing_file_metrics(target)  # measured BEFORE the overwrite
        eol = before[2] if before is not None else DEFAULT_EOL
        bytes_written = _write_text_preserving_eol(target, content, eol=eol)
        return _wrote_observation(path, bytes_written, content=content, before=before)

    def apply_patch(self, patch_text: str) -> str:
        """Apply an OpenCode patch envelope ATOMICALLY (all-or-nothing).

        The WHOLE patch is validated first -- every section parses, Add targets don't
        already exist, Update/Delete targets DO exist, every Update hunk's anchor/
        context locates, and a Move-to target is free. Only if all of that holds is
        ANYTHING written; on any failure NOTHING is applied and a clear observation
        names the failing section. The read-before-edit guard for Update sections is
        enforced by the caller (``relay.loop``) BEFORE this runs.

        Returns a concise summary, e.g.
        ``applied patch: +hello.txt, ~src/main.py (renamed from src/app.py), -obsolete.txt``.
        """
        try:
            sections = parse_patch(patch_text)
        except PatchError as exc:
            return f"apply_patch failed: {exc}"

        # --- validate the WHOLE patch (compute Update results in memory) ---
        updated_text: dict[int, str] = {}
        original_text: dict[int, str] = {}  # to detect no-op Updates (v0.0.31)
        try:
            for idx, sec in enumerate(sections):
                if sec.op == "add":
                    if self.exists(sec.path):
                        raise PatchError(f'Add File "{sec.path}" already exists')
                elif sec.op == "delete":
                    if not self.exists(sec.path):
                        raise PatchError(f'Delete File "{sec.path}" does not exist')
                elif sec.op == "update":
                    if not self.exists(sec.path):
                        raise PatchError(f'Update File "{sec.path}" does not exist')
                    current = self.read(sec.path)
                    original_text[idx] = current
                    updated_text[idx] = _apply_hunks(current, sec.hunks, sec.path)
                    if sec.move_to and sec.move_to != sec.path and self.exists(sec.move_to):
                        raise PatchError(f'Move to "{sec.move_to}" already exists')
        except PatchError as exc:
            return f"apply_patch failed: {exc}"

        # --- apply (every check passed; no half-application possible now) ---
        # A no-op Update -- the hunk LOCATED but produced content identical to the
        # original, with no rename -- is NOT a real change. Writing it back and
        # reporting "applied" would tell the hands the edit took when it did not,
        # sending a capable model into a read->patch->re-read loop (v0.0.31). Skip
        # such sections and report them honestly as "no change" instead.
        summary: list[str] = []
        no_change: list[str] = []
        for idx, sec in enumerate(sections):
            if sec.op == "add":
                target = self._resolve(sec.path)
                target.parent.mkdir(parents=True, exist_ok=True)
                body = "\n".join(sec.add_lines)
                # A new file: default to LF. The model emits LF; a brand-new file has
                # no on-disk EOL to preserve. (If a project prefers CRLF, its existing
                # files dictate that and the next Update on a new file will follow.)
                _write_text_preserving_eol(
                    target, body + ("\n" if sec.add_lines else ""), eol=DEFAULT_EOL
                )
                summary.append(f"+{sec.path}")
            elif sec.op == "delete":
                self._resolve(sec.path).unlink()
                summary.append(f"-{sec.path}")
            else:  # update (with optional rename)
                is_move = bool(sec.move_to and sec.move_to != sec.path)
                if updated_text[idx] == original_text[idx] and not is_move:
                    no_change.append(sec.path)  # located, but changed nothing
                    continue
                dest = sec.move_to or sec.path
                dest_target = self._resolve(dest)
                dest_target.parent.mkdir(parents=True, exist_ok=True)
                # _apply_hunks preserves the source file's EOL: it produces CRLF text
                # for a CRLF file and LF text for an LF file. Write the bytes back
                # straight through; the EOL is already baked in.
                dest_target.write_bytes(updated_text[idx].encode("utf-8"))
                if is_move:
                    self._resolve(sec.path).unlink()
                    summary.append(f"~{dest} (renamed from {sec.path})")
                else:
                    summary.append(f"~{sec.path}")
        if not summary:
            # The WHOLE patch was a no-op: nothing on disk changed. Report a clear
            # NON-success ("applied patch" prefix withheld) so the hands does not treat
            # it as done and re-loop -- it must do something different.
            targets = ", ".join(no_change) or "the target file(s)"
            return f"apply_patch: no change -- {targets} already matches the patch"
        out = "applied patch: " + ", ".join(summary)
        if no_change:
            out += " (no change: " + ", ".join(no_change) + ")"
        return out

    def mkdir(self, path: str) -> str:
        """Create directory ``path`` and any parents, idempotently (cross-platform).

        Uses :func:`os.makedirs` with ``exist_ok=True`` -- NO shell, so it works on
        every OS (unlike ``mkdir -p``, which fails on Windows where ``-p`` is read as
        a folder name). Creates directories ONLY (never files); a second call on an
        existing directory is a clean "already exists", not an error.
        """
        target = self._resolve(path)
        if target.exists():
            if target.is_dir():
                return f"directory already exists: {path}"
            raise ToolError(f"{path} exists and is not a directory")
        os.makedirs(target, exist_ok=True)
        return f"created directory {path}"

    def webfetch(self, url: str) -> str:
        """Fetch ``url`` and return its readable text (HTML stripped to main text).

        The one network-touching tool: read-only (GET), bounded
        (:data:`WEBFETCH_CHAR_CAP` with a truncation note), timed out
        (:data:`WEBFETCH_TIMEOUT_S`), and friendly-on-error -- a failure returns a
        concise ``webfetch failed: ...`` observation, never a raw traceback/blob, so
        the loop can adapt. Only http(s) URLs are accepted (case-insensitive scheme).
        """
        url = (url or "").strip()
        # Case-insensitive scheme check: models sometimes emit ``HTTP://`` or
        # ``Https://`` -- reject only genuinely non-http(s) schemes.
        url_lower = url.lower()
        if not (url_lower.startswith("http://") or url_lower.startswith("https://")):
            return f"webfetch failed: only http(s) URLs are supported (got {url!r})"
        try:
            raw = _http_get(url)
        except Exception as exc:  # noqa: BLE001 -- any fetch failure -> a friendly note
            return f"webfetch failed: could not fetch {url} ({_short_fetch_reason(exc)})"
        text = _html_to_text(raw)
        if len(text) > WEBFETCH_CHAR_CAP:
            text = text[:WEBFETCH_CHAR_CAP] + f"\n... (truncated at {WEBFETCH_CHAR_CAP} chars)"
        # A fetched page can echo headers / tokens / API keys in its body. Mask
        # any of the parent's process env values before returning to the model.
        return _redact_observation(
            text or "(empty response)",
            extra_secrets=_scrubbed_secrets_from_env(),
        )

    # -- read-tracking support (v0.0.23: the read-before-edit guard) ----------
    #
    # Freshness is CONTENT-based, never mtime: the user is on Windows where mtime
    # is unreliable, and a content hash is the robust "has this file changed since
    # I read it?" signal. These three are the filesystem primitives the executor's
    # guard (``relay.loop``) calls; they never raise (a bad path -> a benign answer).

    def exists(self, path: str) -> bool:
        """Whether ``path`` resolves (inside the root) to an existing regular file."""
        try:
            target = self._resolve(path)
        except PathEscapeError:
            return False
        return target.exists() and target.is_file()

    def content_hash(self, path: str) -> str | None:
        """sha256 hex of the file's bytes, or ``None`` when it cannot be hashed
        (missing, a directory, escapes the root, or unreadable)."""
        try:
            target = self._resolve(path)
        except PathEscapeError:
            return None
        if not target.exists() or target.is_dir():
            return None
        try:
            return hashlib.sha256(target.read_bytes()).hexdigest()
        except OSError:
            return None

    def canonical(self, path: str) -> str:
        """A stable read-tracking key for ``path`` (its resolved absolute path when
        inside the root, else the raw string), so ``./a`` and ``a`` map to one entry."""
        try:
            return str(self._resolve(path))
        except PathEscapeError:
            return path

    def bash(self, command: str) -> str:
        """Run ``command`` (cwd pinned to ``project_root``) subject to the policy.

        The verdict from :func:`relay.policy.classify` decides the outcome:
          - ``BLOCKED`` -> never runs; returns a ``BLOCKED by policy: ...``
            observation so the loop can route around it (no exception raised).
          - ``CONFIRM`` -> consults the approver; denied -> returns a
            ``DENIED ...`` observation and does not run.
          - ``ALLOW`` (or an approved ``CONFIRM``) -> runs and returns combined
            stdout/stderr/exit.

        A command that exceeds ``bash_timeout_s`` is killed and a friendly
        ``bash timed out after Ns`` observation is returned -- so a hung command
        (a server, a REPL, ``tail -f``) can't orphan the worker thread forever.

        This is a best-effort policy, NOT a sandbox (see module docstring).
        """
        verdict = classify(command)

        if verdict.verdict == BLOCKED:
            return f"BLOCKED by policy: {verdict.reason}"

        if verdict.verdict == CONFIRM:
            if self.auto_approve:
                pass  # approved without asking
            elif self.approver is not None:
                if not self.approver(command, verdict.reason):
                    return f"DENIED by user: {verdict.reason}"
            else:
                # Safe default for non-interactive contexts: deny, visibly.
                return f"DENIED (no approver; safe default): {verdict.reason}"

        # v0.0.32: the timeout-kill path. ``subprocess.run(timeout=...)`` only
        # stops WAITING after the timeout -- it doesn't actually kill the child
        # (and certainly not the child's children: a hanging ``python -m
        # http.server`` the shell spawned, a forked compiler, ...). We use
        # :class:`Popen` directly so we own the child handle and can walk the
        # descendant tree on timeout (POSIX ``killpg``, Windows ``taskkill /T``).
        proc: subprocess.Popen[bytes] | None = None
        try:
            proc = subprocess.Popen(
                command,
                shell=True,
                cwd=str(self.project_root.resolve()),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                # v0.0.32: decode output as UTF-8 with ``errors="replace"`` (the
                # locale default on Windows is cp1252, which would mojibake any
                # non-Latin output and crash outright on a stray non-cp1252
                # byte). Read the raw bytes and decode ourselves so we get
                # ``errors="replace"`` semantics (Popen doesn't accept those
                # kwargs directly).
                env=_scrubbed_env(),
                # v0.0.32: own process group so the timeout-kill can walk it.
                start_new_session=True,
            )
            try:
                stdout_b, stderr_b = proc.communicate(timeout=self.bash_timeout_s)
            except subprocess.TimeoutExpired:
                timeout_desc = f"{self.bash_timeout_s}s" if self.bash_timeout_s is not None else "(unbounded)"
                snippet = command[:100]
                _kill_process_tree(proc)
                # Reap the killed process so we don't leave a zombie on POSIX.
                try:
                    proc.communicate(timeout=2)
                except (subprocess.TimeoutExpired, OSError):
                    pass
                return f"bash timed out after {timeout_desc} (command: {snippet})"
            stdout = stdout_b.decode("utf-8", errors="replace")
            stderr = stderr_b.decode("utf-8", errors="replace")
            parts: list[str] = []
            if stdout:
                parts.append(stdout.rstrip("\n"))
            if stderr:
                parts.append("[stderr]\n" + stderr.rstrip("\n"))
            parts.append(f"[exit {proc.returncode}]")
            return _cap_observation(
                _redact_observation(
                    "\n".join(parts), extra_secrets=_scrubbed_secrets_from_env()
                )
            )
        finally:
            # Best-effort: if the Popen somehow still has an open handle
            # (an exception path between Popen and communicate), close it.
            if proc is not None and proc.stdout is not None:
                try:
                    proc.stdout.close()
                except OSError:
                    pass
            if proc is not None and proc.stderr is not None:
                try:
                    proc.stderr.close()
                except OSError:
                    pass
            # Reap on any non-timeexit path; communicate() above handles the
            # normal path. (A child that completed but raised in our code
            # before communicate would otherwise be a zombie until the
            # process exited.)
            if proc is not None and proc.poll() is None:
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    _kill_process_tree(proc)
