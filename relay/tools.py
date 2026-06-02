"""The executor's hands: the tools the agent loop can invoke.

Each tool resolves paths relative to a ``project_root`` and refuses to touch
anything outside it. That path-confinement check is the ONLY safety here.

NOTE: There is intentionally NO command denylist / confirmation policy in
v0.02. ``bash`` runs commands as-is with cwd pinned to ``project_root``. The
guardrail layer (denylist, approval prompts, sandboxing) is the v0.03 milestone.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


class ToolError(Exception):
    """A tool could not complete (missing file, bad path, etc.)."""


class PathEscapeError(ToolError):
    """A path resolved outside ``project_root`` and was refused."""


@dataclass
class Tools:
    """Filesystem + shell tools confined to ``project_root``."""

    project_root: Path

    def __post_init__(self) -> None:
        self.project_root = Path(self.project_root)

    def _resolve(self, path: str) -> Path:
        """Resolve ``path`` against the root, refusing anything that escapes it."""
        root = self.project_root.resolve()
        target = (root / path).resolve()
        if target != root and root not in target.parents:
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

    def bash(self, command: str) -> str:
        """Run ``command`` with cwd = ``project_root``; combine stdout/stderr/exit.

        MINIMAL SAFETY ONLY: no denylist or confirmation policy here -- that is
        the v0.03 guardrail milestone.
        """
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
