"""Orchestra mode: parallel hands on disjoint-file plan steps (D4).

Pragmatic v1 (not distributed): schedule independent steps in a thread pool with
file leases. Overlapping path claims are detected before a second write and those
steps fall back to the serial brain path. Cancel joins workers.

Telemetry: worker model calls use role ``hands-N`` (canonicalized to the hands
model in :class:`~relay.config.ModelConfig`); cost still aggregates on one ledger.
"""

from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

# Paths that look like files in step instructions (heuristic claims).
_PATH_TOKEN = re.compile(
    r"""(?<![A-Za-z0-9_])"""
    r"""((?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.[A-Za-z0-9]+)"""
    r"""(?![A-Za-z0-9_])"""
)
_PATH_ATTR = re.compile(
    r"""\bpath\s*=\s*["']([^"']+)["']""",
    re.IGNORECASE,
)


def extract_path_claims(instruction: str) -> set[str]:
    """Heuristic file-path claims from a step instruction.

    Steps with *no* extractable paths are treated as contested (serial-only) so
    we never guess parallel safety.
    """
    text = instruction or ""
    claims: set[str] = set()
    for match in _PATH_ATTR.finditer(text):
        claims.add(match.group(1).lstrip("./"))
    for match in _PATH_TOKEN.finditer(text):
        token = match.group(1).lstrip("./")
        # Skip obvious non-paths (version numbers like 1.2.3 alone are rare with /).
        if token.count(".") >= 1:
            claims.add(token)
    return claims


def claims_overlap(a: Iterable[str], b: Iterable[str]) -> bool:
    return bool(set(a) & set(b))


@dataclass
class PathLease:
    """Process-wide (per-run) file leases for orchestra workers.

    ``try_claim`` returns False when another worker already holds the path —
    callers should fail the step / serialize rather than write.
    """

    _lock: threading.Lock = field(default_factory=threading.Lock)
    _owner: dict[str, str] = field(default_factory=dict)  # path -> worker id

    def try_claim(self, worker_id: str, paths: Iterable[str]) -> tuple[bool, str | None]:
        """Atomically claim ``paths`` for ``worker_id``.

        Returns ``(ok, contested_path)``. If ``ok`` is False, nothing new was claimed.
        """
        wanted = [p for p in paths if p]
        with self._lock:
            for path in wanted:
                holder = self._owner.get(path)
                if holder is not None and holder != worker_id:
                    return False, path
            for path in wanted:
                self._owner[path] = worker_id
            return True, None

    def release(self, worker_id: str, paths: Iterable[str] | None = None) -> None:
        with self._lock:
            if paths is None:
                drop = [p for p, w in self._owner.items() if w == worker_id]
            else:
                drop = list(paths)
            for path in drop:
                if self._owner.get(path) == worker_id:
                    del self._owner[path]

    def holder(self, path: str) -> str | None:
        with self._lock:
            return self._owner.get(path)


def select_disjoint_batch(
    steps: Sequence,
    *,
    max_workers: int,
    claim_fn: Callable[[object], set[str]] | None = None,
) -> list:
    """Pick up to ``max_workers`` pending steps with pairwise-disjoint claims.

    Steps without claims are excluded (serial). Contested relative to an already
    selected step are skipped for this batch.
    """
    if max_workers <= 1:
        return []
    claim_fn = claim_fn or (lambda s: extract_path_claims(getattr(s, "instruction", "")))
    batch: list = []
    claimed: set[str] = set()
    for step in steps:
        if getattr(step, "status", "pending") != "pending":
            continue
        paths = claim_fn(step)
        if not paths:
            continue  # no claims → serial only
        if claims_overlap(paths, claimed):
            continue
        batch.append(step)
        claimed |= paths
        if len(batch) >= max_workers:
            break
    # A batch of size 1 is not worth parallelizing.
    return batch if len(batch) >= 2 else []


@dataclass
class WorkerResult:
    """Outcome of one orchestra worker running a single step."""

    step_index: int
    success: bool
    summary: str = ""
    failure_reason: str = ""
    touched_paths: list[str] = field(default_factory=list)
    before_snapshots: dict[str, str | None] = field(default_factory=dict)
    calls: int = 0
    worker_role: str = "hands"
    cancelled: bool = False
    contested_path: str | None = None


def run_parallel_steps(
    work_items: list[tuple[object, Callable[[], WorkerResult]]],
    *,
    max_workers: int,
    cancel_check: Callable[[], bool] | None = None,
) -> list[WorkerResult]:
    """Run callables in a thread pool; on cancel, wait for in-flight to finish.

    ``work_items`` is ``(step, zero_arg_callable)``. Returns results in completion
    order (not plan order). Always joins workers (``shutdown(wait=True)``).
    """
    if not work_items:
        return []
    workers = max(1, min(max_workers, len(work_items)))
    results: list[WorkerResult] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fn): step for step, fn in work_items}
        try:
            for fut in as_completed(futures):
                if cancel_check is not None and cancel_check():
                    # Don't cancel futures mid-call (money-leak guard); just stop
                    # collecting new work — shutdown(wait=True) joins the rest.
                    break
                try:
                    results.append(fut.result())
                except Exception as exc:  # noqa: BLE001 — surface as failed worker
                    step = futures[fut]
                    results.append(
                        WorkerResult(
                            step_index=getattr(step, "index", -1),
                            success=False,
                            failure_reason=f"orchestra worker error: {exc}",
                        )
                    )
        finally:
            # Explicit join: wait for in-flight model calls / bash to finish.
            pool.shutdown(wait=True, cancel_futures=False)
    return results


def hands_role_for_worker(worker_slot: int) -> str:
    """Telemetry role suffix: hands-1, hands-2, … (slot is 1-based)."""
    return f"hands-{max(1, worker_slot)}"
