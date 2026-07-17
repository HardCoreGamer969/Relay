# 08 — Protocol Fitness Lab

**Shipped:** features-revamp (`relay probe`, offline fixture grading)  
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
- `--fixture` / fixtures under `tests/fixtures/protocol_lab/` and `relay/probes/`
- Scorecard dimensions + overall 0–100; exit codes: **0=fit**, **2=weak**, **3=unfit**
- v1: **offline grading only** (live probe deferred)

## Hooks into existing code

- `relay/probe.py` + `protocol.py` parsers
- Hermetic mock path for unit tests; live probe is out of v1

## Acceptance criteria

- [x] Probe returns graded dimensions + overall fitness 0–100 with rationale strings
- [x] Hands probe never requires write tools against a real user repo (fixture transcripts)
- [x] Documented exit codes: fit / weak / unfit
- [x] Offline tests cover grading of recorded transcripts

## Open questions

- Publish matrix in-repo vs website-only?
- How often to invalidate scores when protocol version changes?

## Out of scope (v1)

- Hosted public leaderboard with community submissions
- Live budget-capped probes (opt-in later)
