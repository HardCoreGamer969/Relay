# 12 — Orchestra × Router

**Phase:** E10 · **Status:** [MASTER](MASTER.md) only  
**Depends on:** D4 orchestra; E2 call-class

## One-liner

Parallel hands workers default to cheapest fit model; contested/replan slices
get the bump — many cheap hands, one expensive conductor.

## User surface

- Orchestra workers inherit `hands_step` call-class (cheap)
- Replan/conductor stays on brain call-class (may bump)
- Documented in broker string when `--orchestra` set

## Acceptance (v1)

- [ ] Orchestra hands use hands call-class model
- [ ] Replan still eligible for brain bump
- [ ] Tests: worker role uses economy hands under balanced route

## v1 cuts

- No per-worker different models beyond hands-N telemetry
