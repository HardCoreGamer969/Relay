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
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from relay.policy import BLOCKED, CONFIRM, classify


class ToolError(Exception):
    """A tool could not complete (missing file, bad path, etc.)."""


class PathEscapeError(ToolError):
    """A path resolved outside ``project_root`` and was refused."""


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

    def edit(self, path: str, content: str) -> str:
        """Write ``content`` as the full new contents of ``path``.

        Parent directories are created as needed. (v0.02 is full-file write;
        diff-based edits come in a later milestone.)
        """
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        return f"wrote {path} ({len(content)} bytes, {lines} lines)"

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
