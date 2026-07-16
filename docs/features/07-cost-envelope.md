# 07 — Cost Envelope Contracts

**Phase:** A1 · **Status:** planned · **Depends on:** existing `--max-cost` / step ceilings (extend, don’t reinvent)

[← Master plan](MASTER.md)

---

## One-liner

Before a run, declare a spend/step contract; warn at thresholds; stop with a partial handoff; end with a brain-vs-hands receipt.

## Why it sets Relay apart

Other agents feel open-ended. Relay already bounds loops. Productize budgets as **contracts with receipts** — the honest-agent brand.

## User surface

- `relay run -g … --max-cost 0.40 --max-total-steps 12` (exists) + clearer preflight summary
- Soft warnings at 50% / 80% of envelope (TUI status + stream line)
- On breach: terminal status `cost_limit` / `max_steps` with “what I’d do next” handoff from brain when possible
- End-of-run receipt: brain $, hands $, wasted escalations, $/completed-step
- `/cost` shows envelope remaining, not only spent

## Hooks into existing code

- `RELAY_MAX_COST`, `RELAY_MAX_TOTAL_STEPS`, ledger in telemetry
- Orchestrator step-ceiling / cost-ceiling seams
- TUI status line cost display
- Runlog persistence for receipts

## Acceptance criteria

- [ ] Preflight prints the active envelope before model spend (headless-friendly)
- [ ] 50/80% warnings fire once each per run
- [ ] Breach stops cleanly with receipt + non-zero exit where appropriate
- [ ] Receipt always splits brain vs hands
- [ ] Tests cover warning thresholds and breach without live network

## Open questions

- Hard stop vs “finish current tool call” grace?
- Should profiles set default envelopes?

## Out of scope (v1)

- Billing integrations / team shared wallets
