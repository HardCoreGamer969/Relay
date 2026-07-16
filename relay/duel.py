"""Model bake-off (``relay duel``): same goal across brain×hands pairings.

v1: sequential same-tree runs. Between pairings Relay restores the worktree via
``git checkout`` / ``git clean`` when ``root`` is a git repo. A dirty tree at
start, or a failed restore, fails closed (remaining pairings are skipped).
Parallel / worktree isolation is out of scope for v1.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from relay.config import ModelConfig
from relay.orchestrator import run_planned
from relay.telemetry import Ledger

SCHEMA_VERSION = 1

_PAIR_RE = re.compile(
    r"^\s*brain\s*=\s*(?P<brain>[^,]+?)\s*,\s*hands\s*=\s*(?P<hands>.+?)\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Pairing:
    """One brain×hands cell in a bake-off matrix."""

    brain: str
    hands: str

    def label(self) -> str:
        return f"brain={self.brain},hands={self.hands}"


@dataclass
class PairingScore:
    """Scorecard row for one pairing."""

    brain: str
    hands: str
    status: str
    steps: int = 0
    cost_usd: float | None = None
    escalations: int = 0
    wall_time_s: float = 0.0
    error: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class DuelResult:
    """Persisted bake-off scorecard."""

    schema_version: int
    duel_id: str
    timestamp: str
    goal: str
    root: str
    pairings: list[PairingScore] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "duel_id": self.duel_id,
            "timestamp": self.timestamp,
            "goal": self.goal,
            "root": self.root,
            "pairings": [p.as_dict() for p in self.pairings],
            "notes": list(self.notes),
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, ensure_ascii=True) + "\n"


def parse_pair(spec: str) -> Pairing:
    """Parse ``brain=<slug>,hands=<slug>`` (whitespace-tolerant)."""
    match = _PAIR_RE.match(spec or "")
    if not match:
        raise ValueError(
            f"Invalid pair {spec!r}; expected 'brain=<slug>,hands=<slug>'"
        )
    brain = match.group("brain").strip()
    hands = match.group("hands").strip()
    if not brain or not hands:
        raise ValueError(f"Invalid pair {spec!r}: empty brain or hands")
    return Pairing(brain=brain, hands=hands)


def load_matrix(path: str | Path) -> list[Pairing]:
    """Load pairings from a matrix file.

    Accepted forms:
      - JSON list of ``{"brain": "...", "hands": "..."}`` objects, or
        ``{"pairings": [...]}`` wrapper
      - Plain text: one ``brain=...,hands=...`` line per pairing (# comments ok)
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    stripped = text.strip()
    if not stripped:
        raise ValueError(f"Matrix file {path} is empty")
    if stripped.startswith("{") or stripped.startswith("["):
        data = json.loads(stripped)
        if isinstance(data, dict):
            data = data.get("pairings") or data.get("matrix") or []
        if not isinstance(data, list) or not data:
            raise ValueError(f"Matrix file {path} has no pairings")
        out: list[Pairing] = []
        for item in data:
            if isinstance(item, str):
                out.append(parse_pair(item))
            elif isinstance(item, dict):
                brain = str(item.get("brain", "")).strip()
                hands = str(item.get("hands", "")).strip()
                if not brain or not hands:
                    raise ValueError(f"Invalid pairing object: {item!r}")
                out.append(Pairing(brain=brain, hands=hands))
            else:
                raise ValueError(f"Invalid pairing entry: {item!r}")
        return out
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(parse_pair(line))
    if not out:
        raise ValueError(f"Matrix file {path} has no pairings")
    return out


def default_duels_dir(root: str | Path) -> Path:
    return Path(root) / ".relay" / "duels"


def persist_duel(result: DuelResult, root: str | Path) -> Path:
    """Write scorecard JSON under ``.relay/duels/<duel_id>.json``."""
    directory = default_duels_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{result.duel_id}.json"
    path.write_text(result.to_json(), encoding="utf-8")
    return path


def list_duels(root: str | Path) -> list[dict]:
    """Load persisted duel scorecards (newest first)."""
    directory = default_duels_dir(root)
    if not directory.is_dir():
        return []
    rows: list[dict] = []
    for path in sorted(directory.glob("*.json"), reverse=True):
        try:
            rows.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return rows


def _git_ok(root: Path) -> bool:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def git_is_dirty(root: str | Path) -> bool:
    """True when the worktree has uncommitted changes (tracked or untracked)."""
    root = Path(root)
    if not _git_ok(root):
        return False
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True  # fail closed: treat unknown as dirty
    if proc.returncode != 0:
        return True
    return bool(proc.stdout.strip())


def git_restore_worktree(root: str | Path) -> bool:
    """Restore tracked files and remove untracked ones. Returns True on success."""
    root = Path(root)
    if not _git_ok(root):
        return True  # nothing to restore outside a repo
    try:
        checkout = subprocess.run(
            ["git", "-C", str(root), "checkout", "--", "."],
            capture_output=True, text=True, timeout=30,
        )
        clean = subprocess.run(
            ["git", "-C", str(root), "clean", "-fd"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if checkout.returncode != 0 or clean.returncode != 0:
        return False
    return not git_is_dirty(root)


def _steps_from_result(result: Any) -> int:
    plan = getattr(result, "plan", None)
    if plan is None:
        return 0
    steps = getattr(plan, "steps", None) or []
    return len(steps)


def run_duel(
    goal: str,
    project_root: str | Path,
    pairings: list[Pairing],
    *,
    client: Any | None = None,
    auto_approve: bool = True,
    supervise: bool = False,
    max_total_steps: int | None = 20,
    require_clean: bool = True,
    restore_between: bool = True,
    persist: bool = True,
    run_fn: Callable[..., Any] | None = None,
    on_pairing: Callable[[Pairing, PairingScore], None] | None = None,
    **run_kwargs: Any,
) -> DuelResult:
    """Run ``goal`` sequentially for each pairing; return + optionally persist scorecard.

    ``run_fn`` defaults to :func:`relay.orchestrator.run_planned` (injectable for tests).
    Extra ``run_kwargs`` are forwarded to each pairing's run (e.g. ``committed_plan``).
    """
    if len(pairings) < 1:
        raise ValueError("duel needs at least one pairing")
    root = Path(project_root)
    runner = run_fn or run_planned
    now = datetime.now(timezone.utc)
    duel_id = now.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    result = DuelResult(
        schema_version=SCHEMA_VERSION,
        duel_id=duel_id,
        timestamp=now.isoformat(),
        goal=goal,
        root=str(root),
    )

    is_repo = _git_ok(root)
    if require_clean and is_repo and git_is_dirty(root):
        result.notes.append(
            "refused: worktree is dirty; commit or stash before a duel "
            "(v1 restores via git checkout between pairings)"
        )
        score = PairingScore(
            brain=pairings[0].brain,
            hands=pairings[0].hands,
            status="refused_dirty",
            error=result.notes[-1],
        )
        result.pairings.append(score)
        if persist:
            persist_duel(result, root)
        return result
    if not is_repo:
        result.notes.append(
            "root is not a git repo; running sequentially without worktree restore"
        )

    for i, pairing in enumerate(pairings):
        if i > 0 and restore_between and is_repo:
            if not git_restore_worktree(root):
                msg = (
                    f"restore failed after pairing {i}; aborting remaining "
                    f"({len(pairings) - i} left)"
                )
                result.notes.append(msg)
                result.pairings.append(
                    PairingScore(
                        brain=pairing.brain,
                        hands=pairing.hands,
                        status="aborted_dirty",
                        error=msg,
                    )
                )
                break

        models = ModelConfig(brain=pairing.brain, hands=pairing.hands)
        ledger = Ledger()
        start = time.perf_counter()
        try:
            planned = runner(
                goal,
                root,
                models=models,
                ledger=ledger,
                client=client,
                auto_approve=auto_approve,
                supervise=supervise,
                max_total_steps=max_total_steps,
                **run_kwargs,
            )
            wall = time.perf_counter() - start
            score = PairingScore(
                brain=pairing.brain,
                hands=pairing.hands,
                status=getattr(planned, "status", "unknown"),
                steps=_steps_from_result(planned),
                cost_usd=ledger.total_cost(),
                escalations=int(getattr(planned, "escalations", 0)),
                wall_time_s=round(wall, 4),
            )
        except Exception as exc:  # noqa: BLE001 — scorecard must record the failure
            wall = time.perf_counter() - start
            score = PairingScore(
                brain=pairing.brain,
                hands=pairing.hands,
                status="error",
                cost_usd=ledger.total_cost(),
                wall_time_s=round(wall, 4),
                error=str(exc),
            )
        result.pairings.append(score)
        if on_pairing is not None:
            on_pairing(pairing, score)

    if persist:
        persist_duel(result, root)
    return result
