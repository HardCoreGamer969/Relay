"""Assumption profiles: named judgment presets over the dial + run defaults."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import relay.store as store

PROFILES = ("surgeon", "contractor", "intern", "chaos")
DEFAULT_PROFILE = "contractor"


@dataclass(frozen=True)
class AssumptionProfile:
    name: str
    assumption_level: str
    confirm_plan: bool = False
    supervise: bool = True
    max_total_steps_hint: int | None = None
    max_cost_hint: float | None = None
    description: str = ""


_BUILTINS: dict[str, AssumptionProfile] = {
    "surgeon": AssumptionProfile(
        name="surgeon",
        assumption_level="5",
        confirm_plan=True,
        supervise=True,
        max_total_steps_hint=30,
        description="Ask early, tiny plans, confirm before execution",
    ),
    "contractor": AssumptionProfile(
        name="contractor",
        assumption_level="3",
        confirm_plan=False,
        supervise=True,
        description="Assume conventions; escalate on product calls",
    ),
    "intern": AssumptionProfile(
        name="intern",
        assumption_level="4",
        confirm_plan=False,
        supervise=True,
        max_total_steps_hint=40,
        description="Over-investigate; never invent APIs",
    ),
    "chaos": AssumptionProfile(
        name="chaos",
        assumption_level="1",
        confirm_plan=False,
        supervise=False,
        max_total_steps_hint=80,
        description="Aggressive assumptions; max budget; throwaway spikes",
    ),
}


def get_profile(name: str) -> AssumptionProfile | None:
    return _BUILTINS.get(str(name).strip().lower())


def resolve_profile(
    override: str | None = None,
    *,
    root: str | Path | None = None,
    config: dict | None = None,
) -> AssumptionProfile:
    """Resolve profile: CLI override > repo `.relay/profile.json` > env > config > default.

    Builtins only in v1. Unknown names fall through.
    """
    for candidate in (
        override,
        _repo_profile_name(root) if root is not None else None,
        os.environ.get("RELAY_PROFILE"),
    ):
        if not candidate:
            continue
        profile = get_profile(str(candidate))
        if profile is not None:
            return profile
    config = config if config is not None else store.load_config()
    if isinstance(config, dict):
        profile = get_profile(str(config.get("profile") or ""))
        if profile is not None:
            return profile
    return _BUILTINS[DEFAULT_PROFILE]


def _repo_profile_name(root: str | Path | None) -> str | None:
    if root is None:
        return None
    path = Path(root) / ".relay" / "profile.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, dict):
        return data.get("profile") or data.get("name")
    if isinstance(data, str):
        return data
    return None


def write_repo_profile(root: str | Path, name: str) -> Path:
    profile = get_profile(name)
    if profile is None:
        raise ValueError(f"unknown profile: {name}")
    path = Path(root) / ".relay" / "profile.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"profile": profile.name}, indent=2) + "\n", encoding="utf-8"
    )
    return path
