"""The executor's hands: the tools the agent loop can invoke.

Each filesystem tool resolves paths relative to a ``project_root`` and refuses
to touch anything outside it (resolve-then-check; see ``_resolve``).

``bash`` additionally consults the command policy (``relay.policy``) before
running anything: it refuses ``BLOCKED`` commands outright and gates
``CONFIRM`` commands behind an approver callback. NOTE the honest limit -- the
policy is a best-effort speed bump against obvious destructive commands, NOT a
security sandbox. cwd-pinning + string classification do not contain an
adversarial command (env expansion, command substitution, eval, etc. evade
both). Real isolation (process/container sandboxing) is a later milestone (v0.95).
"""

from __future__ import annotations

import hashlib
import html
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

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


def _wrote_observation(path: str, content: str) -> str:
    """The shared ``wrote <path> (<bytes> bytes, <lines> lines)`` line for edit/write."""
    lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
    return f"wrote {path} ({len(content)} bytes, {lines} lines)"


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


def _find_line(lines: list[str], anchor: str, start: int = 0) -> int:
    """Index of the first line at/after ``start`` equal to ``anchor`` (exact, then
    whitespace-stripped). ``-1`` if not found."""
    for i in range(start, len(lines)):
        if lines[i] == anchor:
            return i
    target = anchor.strip()
    for i in range(start, len(lines)):
        if lines[i].strip() == target:
            return i
    return -1


def _find_block(lines: list[str], block: list[str], start: int) -> int:
    """Index of the first contiguous occurrence of ``block`` at/after ``start``
    (exact, then whitespace-stripped per line). ``-1`` if not found."""
    m = len(block)
    for i in range(start, len(lines) - m + 1):
        if lines[i:i + m] == block:
            return i
    stripped = [b.strip() for b in block]
    for i in range(start, len(lines) - m + 1):
        if [ln.strip() for ln in lines[i:i + m]] == stripped:
            return i
    return -1


def _apply_hunks(original: str, hunks: list[_Hunk], path: str) -> str:
    """Apply ``hunks`` to ``original`` text, returning the new text.

    Raises :class:`PatchError` if an anchor or a hunk's context block does not
    locate -- the caller relies on this for atomic validation (a non-locating hunk
    fails the WHOLE patch before anything is written)."""
    trailing_nl = original.endswith("\n")
    lines = original.split("\n")
    if trailing_nl:
        lines = lines[:-1]  # drop the empty tail from the trailing newline
    for hunk in hunks:
        start = 0
        anchored = bool(hunk.anchor)
        if anchored:
            idx = _find_line(lines, hunk.anchor)
            if idx == -1:
                raise PatchError(f'Update File "{path}": anchor not found: "@@ {hunk.anchor}"')
            start = idx  # search INCLUSIVE of the anchor: it may be the first changed line
        if hunk.before:
            pos = _find_block(lines, hunk.before, start)
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
    """

    project_root: Path
    approver: Callable[[str, str], bool] | None = None
    auto_approve: bool = False

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
        """
        root = self.project_root.resolve()
        target = (root / path).resolve()
        if not target.is_relative_to(root):
            raise PathEscapeError(
                f"path {path!r} resolves outside the project root and was refused"
            )
        return target

    def read(self, path: str) -> str:
        """Return the contents of a file."""
        target = self._resolve(path)
        if not target.exists():
            raise ToolError(f"no such file: {path}")
        if target.is_dir():
            raise ToolError(f"{path} is a directory, not a file")
        return target.read_text(encoding="utf-8")

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
                text = f.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue  # skip binary / unreadable files
            prefix = f"{f.relative_to(root).as_posix()}:" if multi else ""
            for lineno, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    out.append(f"{prefix}{lineno}: {line}")
        return "\n".join(out) if out else "(no matches)"

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
        diff-based edits come in a later milestone.)
        """
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return _wrote_observation(path, content)

    def write(self, path: str, content: str) -> str:
        """Write ``content`` as the WHOLE contents of ``path`` (create or overwrite).

        Distinct from ``edit`` in intent -- ``write`` is the create-or-replace-an-
        entire-file tool. Parent directories are created as needed. The read-before
        -edit guard (in :mod:`relay.loop`) exempts creating a NEW file but requires a
        current read before overwriting an EXISTING one (you shouldn't blind-clobber a
        file you haven't seen); a successful write invalidates the recorded read.
        """
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return _wrote_observation(path, content)

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
                    updated_text[idx] = _apply_hunks(self.read(sec.path), sec.hunks, sec.path)
                    if sec.move_to and sec.move_to != sec.path and self.exists(sec.move_to):
                        raise PatchError(f'Move to "{sec.move_to}" already exists')
        except PatchError as exc:
            return f"apply_patch failed: {exc}"

        # --- apply (every check passed; no half-application possible now) ---
        summary: list[str] = []
        for idx, sec in enumerate(sections):
            if sec.op == "add":
                target = self._resolve(sec.path)
                target.parent.mkdir(parents=True, exist_ok=True)
                body = "\n".join(sec.add_lines)
                target.write_text(body + ("\n" if sec.add_lines else ""), encoding="utf-8")
                summary.append(f"+{sec.path}")
            elif sec.op == "delete":
                self._resolve(sec.path).unlink()
                summary.append(f"-{sec.path}")
            else:  # update (with optional rename)
                dest = sec.move_to or sec.path
                dest_target = self._resolve(dest)
                dest_target.parent.mkdir(parents=True, exist_ok=True)
                dest_target.write_text(updated_text[idx], encoding="utf-8")
                if sec.move_to and sec.move_to != sec.path:
                    self._resolve(sec.path).unlink()
                    summary.append(f"~{dest} (renamed from {sec.path})")
                else:
                    summary.append(f"~{sec.path}")
        return "applied patch: " + ", ".join(summary)

    def webfetch(self, url: str) -> str:
        """Fetch ``url`` and return its readable text (HTML stripped to main text).

        The one network-touching tool: read-only (GET), bounded
        (:data:`WEBFETCH_CHAR_CAP` with a truncation note), timed out
        (:data:`WEBFETCH_TIMEOUT_S`), and friendly-on-error -- a failure returns a
        concise ``webfetch failed: ...`` observation, never a raw traceback/blob, so
        the loop can adapt. Only http(s) URLs are accepted.
        """
        url = (url or "").strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            return f"webfetch failed: only http(s) URLs are supported (got {url!r})"
        try:
            raw = _http_get(url)
        except Exception as exc:  # noqa: BLE001 -- any fetch failure -> a friendly note
            return f"webfetch failed: could not fetch {url} ({_short_fetch_reason(exc)})"
        text = _html_to_text(raw)
        if len(text) > WEBFETCH_CHAR_CAP:
            return text[:WEBFETCH_CHAR_CAP] + f"\n... (truncated at {WEBFETCH_CHAR_CAP} chars)"
        return text if text else "(empty response)"

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

        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(self.project_root.resolve()),
            capture_output=True,
            text=True,
        )
        parts: list[str] = []
        if proc.stdout:
            parts.append(proc.stdout.rstrip("\n"))
        if proc.stderr:
            parts.append("[stderr]\n" + proc.stderr.rstrip("\n"))
        parts.append(f"[exit {proc.returncode}]")
        return "\n".join(parts)
