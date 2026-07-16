"""Diff-as-interface / surgical commit + rewind (D3).

After a successful step, optionally show a unified diff of touched paths and
require accept/reject before continuing. Reject returns control to brain replan
(step marked failed with the rejection reason). Optional ``--commit-per-step``
makes a git commit from the step instruction (requires a clean-enough git repo).

``relay rewind <step-id>`` restores touched files via ``git checkout`` when git
is available; otherwise fails clearly.
"""

from __future__ import annotations

import difflib
import os
import re
import subprocess
from pathlib import Path
from typing import Callable

from relay.plan_fork import load_checkpoint, list_checkpoints
from relay.store import load_config

_TRUE = ("1", "true", "yes", "on")


def resolve_confirm_diff(override: bool | None = None, config: dict | None = None) -> bool:
    """override > env RELAY_CONFIRM_DIFF > config diff.confirm > False."""
    if override is not None:
        return bool(override)
    env = str(os.environ.get("RELAY_CONFIRM_DIFF", "")).strip().lower()
    if env in _TRUE:
        return True
    if env in ("0", "false", "no", "off"):
        return False
    config = config if config is not None else load_config()
    diff = config.get("diff") if isinstance(config, dict) else None
    if isinstance(diff, dict) and "confirm" in diff:
        return bool(diff["confirm"])
    if isinstance(config, dict) and "confirm_diff" in config:
        return bool(config["confirm_diff"])
    return False


def resolve_commit_per_step(override: bool | None = None, config: dict | None = None) -> bool:
    """override > env RELAY_COMMIT_PER_STEP > config diff.commit_per_step > False."""
    if override is not None:
        return bool(override)
    env = str(os.environ.get("RELAY_COMMIT_PER_STEP", "")).strip().lower()
    if env in _TRUE:
        return True
    if env in ("0", "false", "no", "off"):
        return False
    config = config if config is not None else load_config()
    diff = config.get("diff") if isinstance(config, dict) else None
    if isinstance(diff, dict) and "commit_per_step" in diff:
        return bool(diff["commit_per_step"])
    if isinstance(config, dict) and "commit_per_step" in config:
        return bool(config["commit_per_step"])
    return False


def _git_ok(root: Path) -> bool:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def read_file_or_none(root: str | Path, rel: str) -> str | None:
    path = Path(root) / rel
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        try:
            return path.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            return None


def unified_diff_for_path(
    path: str,
    before: str | None,
    after: str | None,
) -> str:
    """Unified diff for one path. ``None`` means the file was absent."""
    before_lines = ([] if before is None else before.splitlines(keepends=True))
    after_lines = ([] if after is None else after.splitlines(keepends=True))
    from_name = "/dev/null" if before is None else f"a/{path}"
    to_name = "/dev/null" if after is None else f"b/{path}"
    return "".join(
        difflib.unified_diff(
            before_lines, after_lines, fromfile=from_name, tofile=to_name, n=3,
        )
    )


def step_unified_diff(
    root: str | Path,
    touched_paths: list[str],
    before_snapshots: dict[str, str | None] | None = None,
) -> str:
    """Build a multi-file unified diff for paths touched in a step.

    Prefer ``before_snapshots`` (hermetic). Fall back to ``git diff`` for a path
    when no snapshot is available and the root is a git repo.
    """
    root = Path(root)
    before_snapshots = before_snapshots or {}
    chunks: list[str] = []
    for rel in touched_paths:
        before = before_snapshots.get(rel, _SENTINEL)
        if before is _SENTINEL:
            # No snapshot: try git, else treat as unknown→current.
            git_chunk = _git_diff_path(root, rel)
            if git_chunk:
                chunks.append(git_chunk)
                continue
            before = None
        after = read_file_or_none(root, rel)
        chunk = unified_diff_for_path(rel, before if before is not _SENTINEL else None, after)
        if chunk:
            chunks.append(chunk)
    return "\n".join(chunks) if chunks else "(no textual diff)"


_SENTINEL = object()


def _git_diff_path(root: Path, rel: str) -> str:
    if not _git_ok(root):
        return ""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "diff", "--", rel],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode not in (0, 1):
        return ""
    return (proc.stdout or "").strip()


def parse_diff_decision(answer: str) -> str:
    """Map a user_decision answer to ``accept`` | ``reject``.

    Accept: empty, y/yes/accept/a/ok/continue. Reject: n/no/reject/r/edit.
    Ambiguous text defaults to reject (fail closed on unclear).
    """
    text = (answer or "").strip().lower()
    if text in ("", "y", "yes", "accept", "a", "ok", "continue", "lgtm"):
        return "accept"
    if text in ("n", "no", "reject", "r", "edit", "edit-request", "replan"):
        return "reject"
    if text.startswith("reject") or text.startswith("no"):
        return "reject"
    if text.startswith("accept") or text.startswith("yes"):
        return "accept"
    return "reject"


def confirm_step_diff(
    *,
    root: str | Path,
    step_index: int,
    instruction: str,
    touched_paths: list[str],
    before_snapshots: dict[str, str | None] | None,
    user_decision: Callable[[str], str],
) -> tuple[bool, str]:
    """Show diff via the decision prompt; return (accepted, reason)."""
    diff = step_unified_diff(root, touched_paths, before_snapshots)
    paths = ", ".join(touched_paths) if touched_paths else "(none)"
    prompt = (
        f"Step {step_index} diff confirm — accept to keep, reject to replan.\n"
        f"instruction: {instruction}\n"
        f"touched: {paths}\n"
        f"--- diff ---\n{diff}\n---\n"
        "Reply accept/reject (or y/n):"
    )
    answer = user_decision(prompt)
    decision = parse_diff_decision(answer)
    if decision == "accept":
        return True, (answer or "accept").strip()
    reason = (answer or "").strip() or "diff rejected by user"
    if reason.lower() in ("n", "no", "reject", "r"):
        reason = "diff rejected by user"
    return False, reason


def commit_step_changes(
    root: str | Path,
    *,
    step_index: int,
    instruction: str,
    touched_paths: list[str],
) -> str:
    """``git add`` touched paths and commit with a message from the step.

    Requires a git repo. Returns the new commit hash (or raises ``RuntimeError``).
    """
    root = Path(root)
    if not _git_ok(root):
        raise RuntimeError(
            "commit-per-step requires a git repository "
            "(initialize with `git init` or disable --commit-per-step)"
        )
    if not touched_paths:
        return ""
    msg = f"relay step {step_index}: {instruction.strip()}"[:200]
    try:
        add = subprocess.run(
            ["git", "-C", str(root), "add", "--", *touched_paths],
            capture_output=True, text=True, timeout=30,
        )
        if add.returncode != 0:
            raise RuntimeError(f"git add failed: {(add.stderr or add.stdout).strip()}")
        commit = subprocess.run(
            ["git", "-C", str(root), "commit", "-m", msg, "--allow-empty-message"],
            capture_output=True, text=True, timeout=30,
        )
        # allow-empty when nothing staged (already committed) — treat as soft ok
        if commit.returncode != 0:
            err = (commit.stderr or commit.stdout or "").strip()
            if "nothing to commit" in err.lower():
                return ""
            raise RuntimeError(f"git commit failed: {err}")
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"git commit-per-step failed: {exc}") from exc
    return (head.stdout or "").strip()


_STEP_ID_RE = re.compile(r"^(?:step[-_])?(\d+)$", re.IGNORECASE)


def parse_step_id(step_id: str) -> int | None:
    match = _STEP_ID_RE.match((step_id or "").strip())
    if not match:
        return None
    return int(match.group(1))


def rewind_step_files(
    root: str | Path,
    step_id: str,
    *,
    checkpoint_id: str | None = None,
) -> list[str]:
    """Restore files touched by ``step_id`` via ``git checkout -- <paths>``.

    Looks up touches from the given checkpoint (or latest / any checkpoint that
    recorded them). Raises ``RuntimeError`` when git is unavailable or the step
    has no recorded touches.
    """
    root = Path(root)
    if not _git_ok(root):
        raise RuntimeError(
            "rewind requires a git repository; "
            "this project is not a git repo (or git is missing)"
        )
    index = parse_step_id(step_id)
    if index is None:
        raise ValueError(f"invalid step id {step_id!r}; expected e.g. '1' or 'step-1'")

    touches = _lookup_touches(root, index, checkpoint_id=checkpoint_id)
    if not touches:
        raise RuntimeError(
            f"no touched paths recorded for step {index}; "
            "cannot rewind (run with checkpoints enabled)"
        )
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "checkout", "--", *touches],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"git checkout failed: {exc}") from exc
    if proc.returncode != 0:
        raise RuntimeError(
            f"git checkout failed: {(proc.stderr or proc.stdout).strip()}"
        )
    return touches


def _lookup_touches(
    root: Path, step_index: int, *, checkpoint_id: str | None
) -> list[str]:
    key = str(step_index)
    candidates: list[str] = []
    if checkpoint_id:
        candidates.append(checkpoint_id)
    else:
        try:
            latest = load_checkpoint(root, "latest")
            candidates.append(latest.id)
        except FileNotFoundError:
            pass
        for row in list_checkpoints(root):
            cid = row.get("id")
            if cid and cid not in candidates:
                candidates.append(cid)
    for cid in candidates:
        try:
            cp = load_checkpoint(root, cid)
        except FileNotFoundError:
            continue
        paths = cp.step_touches.get(key)
        if paths:
            return list(paths)
    return []
