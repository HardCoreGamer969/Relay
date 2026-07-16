# 02 — Assumption Profiles

**Phase:** B1 · **Status:** [MASTER roadmap table](MASTER.md) only  
**Shipped:** features-revamp (builtins + CLI `--profile` / TUI `/profile` / `.relay/profile.json`)  
**Depends on:** existing assumption dial (no hard infra blockers)

## Blockers

- None

---

## One-liner

Named personalities that encode *how aggressive* Relay is — not just which model — and can travel with a repo.

## Why it sets Relay apart

Others ship YOLO / auto-approve toggles. Relay already has a 1–5 assume-vs-ask dial across planning *and* escalations. Profiles make judgment a first-class product surface.

## Suggested profiles (v1)

| Profile | Bias |
|---------|------|
| `surgeon` | Ask early, tiny plans, confirm edits |
| `contractor` | Assume conventions; escalate on product calls |
| `intern` | Over-investigate; never invent APIs |
| `chaos` | Aggressive assumptions; max budget; throwaway spikes |

Each profile maps to dial level + defaults for confirm-plan, supervise, max steps/cost hints, and (later) firewall strictness.

## User surface

- `relay run --profile surgeon` / env `RELAY_PROFILE`
- Repo file: `.relay/profile.json` overrides user default
- TUI: `/profile` shows active profile + underlying dial
- `--assume` still wins for the dial when set

## Hooks into existing code

- `RELAY_ASSUMPTION_LEVEL` and conversational planning assumptions
- Escalation / question paths in orchestrator + bridge
- Config store (`config.json`) + optional project-local overlay

## Acceptance criteria

- [x] Four built-in profiles resolve to deterministic dial + flag defaults
- [x] Precedence documented: CLI > repo file > env > user config > default
- [x] TUI/CLI show which profile is active for a run
- [x] Tests cover resolution precedence and unknown profile fallthrough

## Open questions

- Can users define custom profiles in v1, or builtins only? **v1: builtins only.**
- Does changing profile mid-run (TUI) apply only to the next escalation? **Session-only until next run.**

## Out of scope (v1)

- Learning a profile automatically from user answers (interesting later)
