"""Plan dock helpers for the Relay TUI cockpit (U2)."""

from __future__ import annotations

import os

from rich.text import Text

from .theme import C_DIM, C_GREEN, C_RED, C_TXT, C_MUTED, W_RED, W_TEXT, W_TEXT_DIM, _PLAN_ICON, _SPINNER_FRAMES

PLAN_MODES = ("full", "active", "hidden")
DEFAULT_PLAN_MODE = "full"
NARROW_COLS = 100


def resolve_plan_mode(
    explicit: str | None = None,
    *,
    width: int | None = None,
    pinned_full: bool = False,
    env: dict | None = None,
) -> str:
    """Resolve plan dock mode: explicit > RELAY_TUI_PLAN > default full.

    Narrow terminals coerce ``full`` → ``active`` unless the user pinned ``full``
    (via ``/plan full`` after an auto-coerce, tracked by ``pinned_full``).
    """
    env = env if env is not None else os.environ
    mode = (explicit or env.get("RELAY_TUI_PLAN") or DEFAULT_PLAN_MODE).strip().lower()
    if mode not in PLAN_MODES:
        mode = DEFAULT_PLAN_MODE
    if (
        mode == "full"
        and not pinned_full
        and width is not None
        and width < NARROW_COLS
    ):
        return "active"
    return mode


def visible_plan_indices(plan_steps: list[dict], mode: str) -> list[int]:
    """Which step indices to show for ``mode``."""
    n = len(plan_steps)
    if n == 0 or mode == "hidden":
        return []
    if mode == "full":
        return list(range(n))
    # active: current ± one neighbor
    active = next((i for i, s in enumerate(plan_steps) if s["status"] == "active"), None)
    if active is None:
        # show last settled + remaining pending head
        settled = [i for i, s in enumerate(plan_steps) if s["status"] in ("done", "failed")]
        pending = [i for i, s in enumerate(plan_steps) if s["status"] == "pending"]
        idxs = []
        if settled:
            idxs.append(settled[-1])
        idxs.extend(pending[:2])
        return idxs or list(range(min(3, n)))
    start = max(0, active - 1)
    end = min(n, active + 2)
    return list(range(start, end))


def render_plan_dock(
    plan_steps: list[dict],
    *,
    mode: str,
    spin_frame: int = 0,
) -> Text:
    """Rich Text for the plan dock (or empty when hidden / no plan)."""
    text = Text()
    if mode == "hidden" or not plan_steps:
        return text
    idxs = visible_plan_indices(plan_steps, mode)
    total = len(plan_steps)
    text.append(f"plan · {total} step{'s' if total != 1 else ''}", style=C_DIM)
    if mode == "active" and total > len(idxs):
        text.append("  (focus)", style=C_DIM)
    for i in idxs:
        step = plan_steps[i]
        status = step["status"]
        icon = (
            _SPINNER_FRAMES[spin_frame % len(_SPINNER_FRAMES)]
            if status == "active" else _PLAN_ICON.get(status, "○")
        )
        # Active = bright red accent (website brand); done = green; failed = red.
        icon_style = {
            "done": C_GREEN,
            "active": W_RED,
            "failed": C_RED,
        }.get(status, C_DIM)
        body_style = {
            "active": f"bold {W_TEXT}",
            "done": C_MUTED,
        }.get(status, W_TEXT_DIM)
        marker = "▸ " if status == "active" else "  "
        text.append("\n")
        text.append(marker, style=W_RED if status == "active" else C_DIM)
        text.append(f"{icon} ", style=icon_style)
        text.append(f"{i + 1:02d} ", style=C_DIM)
        text.append(step["instruction"], style=body_style)
    return text
