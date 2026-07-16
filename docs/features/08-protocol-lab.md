# 08 — Protocol Fitness Lab

**Phase:** C2 · **Status:** [MASTER roadmap table](MASTER.md) only  
**Depends on:** stable protocol; pairs with C1; benefits from A2 traces

## Blockers

- None hard; live probes must stay budget-capped and opt-in

---

## One-liner

`relay probe <model>` grades protocol compliance (plan shape, step size, tag discipline, recovery from refusals) and builds a living “Relay fitness” matrix for OpenRouter slugs.

## Why it sets Relay apart

Codex/Claude Code optimize for models with great native tool-calling. Relay’s text-protocol moat is “weird models still work” — **prove it** with a fitness score.

## User surface

- `relay probe <slug> --role brain|hands|both`
- Fixture tasks under `tests/fixtures/protocol_lab/` (or `relay/probes/`)
- Local scorecard JSON; optional export for docs/website later
- CI canary: probe default brain/hands slugs on a schedule (budget-capped)

## Hooks into existing code

- `protocol.py` parsers and malformed-tag feedback
- `models.py` / catalog resolution
- Hermetic mock path for unit tests; live probe is opt-in and budgeted
- Bake-off (#1) can consume fitness as a prefilter

## Acceptance criteria

- [ ] Probe returns graded dimensions + overall fitness 0–100 with rationale strings
- [ ] Hands probe never requires write tools against a real user repo (sandbox fixture dir)
- [ ] Documented exit codes: fit / weak / unfit
- [ ] Offline tests cover grading of recorded transcripts

## Open questions

- Publish matrix in-repo vs website-only?
- How often to invalidate scores when protocol version changes?

## Out of scope (v1)

- Hosted public leaderboard with community submissions
