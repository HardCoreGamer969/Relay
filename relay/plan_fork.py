"""Plan forks + step-boundary checkpoints (D2).

Named plan forks live under ``.relay/forks/`` (plan JSON + metadata).
Step-boundary checkpoints live under ``.relay/checkpoints/`` (plan cursor,
completed steps, optional git commit hash, per-step touched paths).

Resume is thin v1: load a checkpoint/fork into a :class:`~relay.planner.Plan`
with completed steps already marked, then call ``run_planned(committed_plan=...)``.
Full :class:`RunState` session resume is deferred to REVAMP Phase 2.
"""

from __future__ import annotations

import json
import re
import subprocess
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from relay.planner import Plan

SCHEMA_VERSION = 1
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def forks_dir(root: str | Path) -> Path:
    return Path(root) / ".relay" / "forks"


def checkpoints_dir(root: str | Path) -> Path:
    return Path(root) / ".relay" / "checkpoints"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_name(name: str) -> str:
    cleaned = (name or "").strip()
    if not _SAFE_NAME.match(cleaned):
        raise ValueError(
            f"invalid fork name {name!r}; use 1-64 chars of "
            "[A-Za-z0-9._-] starting with alphanumeric"
        )
    return cleaned


def git_head(root: str | Path) -> str | None:
    """Best-effort HEAD commit hash, or None if not a git repo / git missing."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    sha = (proc.stdout or "").strip()
    return sha or None


@dataclass
class ForkRecord:
    """A named alternate plan future."""

    schema_version: int
    name: str
    goal: str
    plan: dict
    created_at: str
    notes: str = ""
    source_checkpoint: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)

    def to_plan(self) -> Plan:
        return Plan.from_state(self.plan)


@dataclass
class CheckpointRecord:
    """Plan state at a step boundary (cursor + completed set + touches)."""

    schema_version: int
    id: str
    goal: str
    plan: dict
    cursor: int  # index of next pending step (or len(steps) if done)
    completed_indices: list[int]
    created_at: str
    git_commit: str | None = None
    # step_index -> paths touched when that step completed
    step_touches: dict[str, list[str]] = field(default_factory=dict)
    # step_index -> unified-diff-friendly before snapshots (path -> content|None)
    step_befores: dict[str, dict[str, str | None]] = field(default_factory=dict)
    status: str = "running"
    notes: str = ""

    def as_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "goal": self.goal,
            "plan": self.plan,
            "cursor": self.cursor,
            "completed_indices": list(self.completed_indices),
            "created_at": self.created_at,
            "git_commit": self.git_commit,
            "step_touches": {k: list(v) for k, v in self.step_touches.items()},
            "step_befores": {
                k: dict(v) for k, v in self.step_befores.items()
            },
            "status": self.status,
            "notes": self.notes,
        }

    def to_plan(self) -> Plan:
        return Plan.from_state(self.plan)


def save_fork(
    root: str | Path,
    name: str,
    plan: Plan,
    *,
    goal: str = "",
    notes: str = "",
    source_checkpoint: str | None = None,
) -> ForkRecord:
    """Persist a named plan fork under ``.relay/forks/<name>.json``."""
    name = _validate_name(name)
    record = ForkRecord(
        schema_version=SCHEMA_VERSION,
        name=name,
        goal=goal,
        plan=plan.to_state(),
        created_at=_now(),
        notes=notes,
        source_checkpoint=source_checkpoint,
    )
    directory = forks_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json"
    path.write_text(json.dumps(record.as_dict(), indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    return record


def load_fork(root: str | Path, name: str) -> ForkRecord:
    name = _validate_name(name)
    path = forks_dir(root) / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"fork not found: {name}")
    data = json.loads(path.read_text(encoding="utf-8"))
    return ForkRecord(
        schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
        name=data.get("name", name),
        goal=data.get("goal", ""),
        plan=data.get("plan") or {"steps": []},
        created_at=data.get("created_at", ""),
        notes=data.get("notes", ""),
        source_checkpoint=data.get("source_checkpoint"),
    )


def list_forks(root: str | Path) -> list[dict[str, Any]]:
    directory = forks_dir(root)
    if not directory.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        steps = (data.get("plan") or {}).get("steps") or []
        rows.append({
            "name": data.get("name", path.stem),
            "goal": data.get("goal", ""),
            "steps": len(steps),
            "created_at": data.get("created_at", ""),
            "notes": data.get("notes", ""),
        })
    return rows


def _cursor_from_plan(plan: Plan) -> int:
    pending = plan.next_pending()
    if pending is None:
        return len(plan.steps)
    return pending.index


def save_checkpoint(
    root: str | Path,
    plan: Plan,
    *,
    goal: str = "",
    checkpoint_id: str | None = None,
    git_commit: str | None = None,
    step_touches: dict[str, list[str]] | None = None,
    step_befores: dict[str, dict[str, str | None]] | None = None,
    status: str = "running",
    notes: str = "",
) -> CheckpointRecord:
    """Write a step-boundary checkpoint under ``.relay/checkpoints/``."""
    cid = checkpoint_id or f"cp-{uuid.uuid4().hex[:12]}"
    completed = [s.index for s in plan.steps if s.status == "done"]
    if git_commit is None:
        git_commit = git_head(root)
    record = CheckpointRecord(
        schema_version=SCHEMA_VERSION,
        id=cid,
        goal=goal,
        plan=plan.to_state(),
        cursor=_cursor_from_plan(plan),
        completed_indices=completed,
        created_at=_now(),
        git_commit=git_commit,
        step_touches=dict(step_touches or {}),
        step_befores=dict(step_befores or {}),
        status=status,
        notes=notes,
    )
    directory = checkpoints_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{cid}.json"
    path.write_text(json.dumps(record.as_dict(), indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    # Also keep a stable "latest" pointer for convenience.
    (directory / "latest.json").write_text(
        json.dumps({"id": cid}, indent=2) + "\n", encoding="utf-8"
    )
    return record


def load_checkpoint(root: str | Path, checkpoint_id: str) -> CheckpointRecord:
    cid = (checkpoint_id or "").strip()
    if cid in ("", "latest"):
        latest = checkpoints_dir(root) / "latest.json"
        if not latest.exists():
            raise FileNotFoundError("no checkpoints yet")
        cid = json.loads(latest.read_text(encoding="utf-8")).get("id", "")
    path = checkpoints_dir(root) / f"{cid}.json"
    if not path.exists():
        raise FileNotFoundError(f"checkpoint not found: {cid}")
    data = json.loads(path.read_text(encoding="utf-8"))
    return CheckpointRecord(
        schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
        id=data.get("id", cid),
        goal=data.get("goal", ""),
        plan=data.get("plan") or {"steps": []},
        cursor=int(data.get("cursor", 0)),
        completed_indices=list(data.get("completed_indices") or []),
        created_at=data.get("created_at", ""),
        git_commit=data.get("git_commit"),
        step_touches={str(k): list(v) for k, v in (data.get("step_touches") or {}).items()},
        step_befores={
            str(k): dict(v) for k, v in (data.get("step_befores") or {}).items()
        },
        status=data.get("status", "running"),
        notes=data.get("notes", ""),
    )


def list_checkpoints(root: str | Path) -> list[dict[str, Any]]:
    directory = checkpoints_dir(root)
    if not directory.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(directory.glob("cp-*.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rows.append({
            "id": data.get("id", path.stem),
            "goal": data.get("goal", ""),
            "cursor": data.get("cursor", 0),
            "completed": len(data.get("completed_indices") or []),
            "status": data.get("status", ""),
            "created_at": data.get("created_at", ""),
            "git_commit": data.get("git_commit"),
        })
    return rows


def plan_for_resume(record: CheckpointRecord | ForkRecord) -> Plan:
    """Return a Plan ready for ``run_planned(committed_plan=...)``.

    Completed steps stay ``done`` so the executor resumes at the next pending.
    """
    return record.to_plan()


def fork_from_checkpoint(
    root: str | Path,
    name: str,
    checkpoint_id: str,
    *,
    notes: str = "",
) -> ForkRecord:
    cp = load_checkpoint(root, checkpoint_id)
    return save_fork(
        root, name, cp.to_plan(),
        goal=cp.goal, notes=notes, source_checkpoint=cp.id,
    )
