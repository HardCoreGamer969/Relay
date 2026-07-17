"""Protocol Fitness Lab (``relay probe``): grade Relay protocol compliance.

v1 is **offline-first**: grade recorded/fixture transcripts for plan shape and
tag discipline. Live probes are opt-in and budget-capped when wired later.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from relay.protocol import parse

# Exit codes for CLI / CI canaries.
EXIT_FIT = 0
EXIT_WEAK = 2
EXIT_UNFIT = 3
EXIT_ERROR = 1

FIT_THRESHOLD = 70
WEAK_THRESHOLD = 40

ROLES = ("brain", "hands", "both")

# Bundled fixtures ship next to this module; tests may override via path.
DEFAULT_FIXTURES_DIR = Path(__file__).resolve().parent / "probes"


@dataclass
class DimensionScore:
    """One graded dimension with a 0–100 score and rationale."""

    name: str
    score: int
    rationale: str
    weight: float = 1.0

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ProbeResult:
    """Fitness scorecard for one slug × role."""

    slug: str
    role: str
    overall: int
    band: str  # fit | weak | unfit
    dimensions: list[DimensionScore] = field(default_factory=list)
    fixture: str = ""
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "slug": self.slug,
            "role": self.role,
            "overall": self.overall,
            "band": self.band,
            "dimensions": [d.as_dict() for d in self.dimensions],
            "fixture": self.fixture,
            "notes": list(self.notes),
        }

    def exit_code(self) -> int:
        if self.band == "fit":
            return EXIT_FIT
        if self.band == "weak":
            return EXIT_WEAK
        return EXIT_UNFIT


def band_for(score: int) -> str:
    if score >= FIT_THRESHOLD:
        return "fit"
    if score >= WEAK_THRESHOLD:
        return "weak"
    return "unfit"


def _clamp(n: int) -> int:
    return max(0, min(100, int(n)))


def _weighted_overall(dimensions: list[DimensionScore]) -> int:
    if not dimensions:
        return 0
    total_w = sum(d.weight for d in dimensions) or 1.0
    raw = sum(d.score * d.weight for d in dimensions) / total_w
    return _clamp(round(raw))


def _load_transcript(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"fixture {path} must be a JSON object")
    return data


def discover_fixtures(directory: str | Path | None = None) -> list[Path]:
    """Return fixture JSON paths under ``directory`` (default: bundled probes)."""
    root = Path(directory) if directory else DEFAULT_FIXTURES_DIR
    if not root.is_dir():
        return []
    return sorted(root.glob("*.json"))


def grade_brain_transcript(turns: list[dict[str, Any]]) -> list[DimensionScore]:
    """Grade brain-side protocol: plan shape + tag discipline."""
    texts = [str(t.get("text") or t.get("content") or "") for t in turns]
    joined = "\n".join(texts)
    parsed_all = [parse(t) for t in texts]

    # --- plan shape ---
    plan_actions = [
        a for pr in parsed_all for a in pr.actions if a.kind == "plan"
    ]
    plan_score = 0
    plan_why = "no <plan> tag found"
    if plan_actions:
        steps = plan_actions[0].steps or []
        n = len(steps)
        if n == 0:
            plan_score, plan_why = 20, "empty <plan>"
        elif n == 1:
            plan_score, plan_why = 55, "single-step plan (acceptable but shallow)"
        elif 2 <= n <= 8:
            # Prefer concrete, non-vague step text.
            vague = sum(
                1
                for s in steps
                if len(s.split()) < 3 or s.lower() in ("do it", "fix", "continue")
            )
            plan_score = 95 if vague == 0 else max(50, 95 - 15 * vague)
            plan_why = f"{n} steps" + (f", {vague} vague" if vague else ", concrete")
        else:
            plan_score, plan_why = 60, f"{n} steps (over-fragmented)"
    # Abort without plan is also a valid brain terminal for unreachable goals.
    elif any(a.kind == "abort" for pr in parsed_all for a in pr.actions):
        plan_score, plan_why = 80, "emitted <abort> (valid when goal unreachable)"

    # --- tag discipline ---
    write_kinds = {"edit", "bash", "write", "apply_patch", "mkdir"}
    writes = [
        a.kind
        for pr in parsed_all
        for a in pr.actions
        if a.kind in write_kinds
    ]
    balanced_tags = len(re.findall(r"<([a-z_]+)\b[^>]*>.*?</\1>", joined, re.DOTALL | re.I))
    naked_opens = len(re.findall(r"<([a-z_]+)\b[^>]*(?<!/)>", joined, re.I)) - balanced_tags
    # Count self-closing as fine; focus on write leakage + unclosed tags.
    disc = 100
    reasons: list[str] = []
    if writes:
        disc -= min(60, 30 * len(writes))
        reasons.append(f"brain emitted write action(s): {writes}")
    if naked_opens > balanced_tags:
        # Rough: many opens without matching closes.
        leak = naked_opens - balanced_tags
        disc -= min(40, 10 * leak)
        reasons.append(f"unbalanced tags (~{leak})")
    # Malformed / empty action turns hurt discipline.
    empty_turns = sum(1 for pr in parsed_all if pr.is_parse_failure and not pr.thinking)
    if empty_turns:
        disc -= min(30, 10 * empty_turns)
        reasons.append(f"{empty_turns} empty/non-protocol turn(s)")
    disc = _clamp(disc)
    disc_why = "; ".join(reasons) if reasons else "tags well-formed; read-only brain surface"

    # --- terminator clarity ---
    terminators = sum(
        1
        for pr in parsed_all
        for a in pr.actions
        if a.kind in ("plan", "abort", "verdict", "decision")
    )
    # Also accept balanced custom terminators in raw text.
    if terminators == 0 and re.search(
        r"<(plan|abort|verdict|decision)\b[^>]*>.*?</\1>", joined, re.DOTALL | re.I
    ):
        terminators = 1
    term_score = 90 if terminators else 25
    term_why = (
        f"{terminators} terminator tag(s)"
        if terminators
        else "no plan/abort/verdict/decision terminator"
    )

    return [
        DimensionScore("plan_shape", _clamp(plan_score), plan_why, weight=1.5),
        DimensionScore("tag_discipline", disc, disc_why, weight=1.5),
        DimensionScore("terminator_clarity", _clamp(term_score), term_why, weight=1.0),
    ]


def grade_hands_transcript(turns: list[dict[str, Any]]) -> list[DimensionScore]:
    """Grade hands-side protocol: action tags + done/blocked discipline."""
    texts = [str(t.get("text") or t.get("content") or "") for t in turns]
    parsed_all = [parse(t) for t in texts]

    actions = [a for pr in parsed_all for a in pr.actions]
    kinds = [a.kind for a in actions]

    # --- action validity ---
    if not actions:
        action_score, action_why = 10, "no protocol actions"
    else:
        failures = sum(1 for pr in parsed_all if pr.is_parse_failure)
        action_score = _clamp(100 - 25 * failures)
        action_why = f"{len(actions)} action(s), {failures} parse-failure turn(s)"

    # --- step termination ---
    terminals = sum(1 for k in kinds if k in ("done", "blocked", "question"))
    if terminals:
        term_score, term_why = 95, f"{terminals} done/blocked/question terminator(s)"
    else:
        term_score, term_why = 20, "no <done>/<blocked>/<question> terminator"

    # --- tool surface (prefer Relay tags over prose tool calls) ---
    prose_tools = sum(
        1
        for t in texts
        if re.search(r"\b(I will |I'll |Let me )(read|edit|run|write)\b", t, re.I)
        and not parse(t).actions
    )
    surface = _clamp(100 - 30 * prose_tools)
    surface_why = (
        "actions expressed as Relay tags"
        if prose_tools == 0
        else f"{prose_tools} prose-only tool turn(s) without tags"
    )

    return [
        DimensionScore("action_validity", action_score, action_why, weight=1.5),
        DimensionScore("step_termination", _clamp(term_score), term_why, weight=1.5),
        DimensionScore("tag_surface", surface, surface_why, weight=1.0),
    ]


def grade_transcript(
    data: dict[str, Any],
    *,
    role: str = "both",
    slug: str | None = None,
) -> ProbeResult:
    """Grade a fixture/transcript dict for ``role`` (brain|hands|both)."""
    role = (role or "both").strip().lower()
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}, got {role!r}")
    slug = slug or str(data.get("slug") or data.get("model") or "unknown")
    dims: list[DimensionScore] = []
    notes: list[str] = []

    brain_turns = data.get("brain") or data.get("brain_turns") or []
    hands_turns = data.get("hands") or data.get("hands_turns") or []
    # Flat ``turns`` with role field also supported.
    if not brain_turns and not hands_turns and "turns" in data:
        for turn in data["turns"]:
            r = str(turn.get("role", "")).lower()
            if r == "brain":
                brain_turns.append(turn)
            elif r == "hands":
                hands_turns.append(turn)

    if role in ("brain", "both"):
        if brain_turns:
            dims.extend(grade_brain_transcript(brain_turns))
        else:
            notes.append("no brain turns in fixture")
            if role == "brain":
                dims.append(DimensionScore("plan_shape", 0, "missing brain transcript", 1.0))
    if role in ("hands", "both"):
        if hands_turns:
            dims.extend(grade_hands_transcript(hands_turns))
        else:
            notes.append("no hands turns in fixture")
            if role == "hands":
                dims.append(DimensionScore("action_validity", 0, "missing hands transcript", 1.0))

    overall = _weighted_overall(dims)
    return ProbeResult(
        slug=slug,
        role=role,
        overall=overall,
        band=band_for(overall),
        dimensions=dims,
        fixture=str(data.get("name") or data.get("fixture") or ""),
        notes=notes,
    )


def probe_fixture(
    path: str | Path,
    *,
    role: str = "both",
    slug: str | None = None,
) -> ProbeResult:
    """Load a fixture JSON and grade it."""
    path = Path(path)
    data = _load_transcript(path)
    result = grade_transcript(data, role=role, slug=slug)
    if not result.fixture:
        result.fixture = path.name
    return result


def probe_offline(
    slug: str,
    *,
    role: str = "both",
    fixtures_dir: str | Path | None = None,
    fixture: str | Path | None = None,
) -> ProbeResult:
    """Grade offline fixtures for ``slug``.

    If ``fixture`` is given, grade that file. Otherwise pick the first fixture
    whose ``slug``/``model`` matches, else the first fixture in the directory
    (useful for CI canaries against a stock good/bad pair).
    """
    if fixture is not None:
        return probe_fixture(fixture, role=role, slug=slug)

    paths = discover_fixtures(fixtures_dir)
    if not paths:
        result = ProbeResult(
            slug=slug,
            role=role,
            overall=0,
            band="unfit",
            notes=["no fixtures found"],
        )
        return result

    matched: Path | None = None
    for path in paths:
        try:
            data = _load_transcript(path)
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        cand = str(data.get("slug") or data.get("model") or "")
        if cand == slug or slug in path.stem:
            matched = path
            break
    target = matched or paths[0]
    result = probe_fixture(target, role=role, slug=slug)
    if matched is None:
        result.notes.append(
            f"no fixture matched slug {slug!r}; graded {target.name}"
        )
    return result
