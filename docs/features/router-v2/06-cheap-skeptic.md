# 06 — Cheap Skeptic Assassin

**Phase:** E6 · **Status:** [MASTER](MASTER.md) only  
**Depends on:** D1 `--skeptic`; router call-class

## One-liner

Skeptic always runs on a fixed cheap model; only escalate skeptic if it finds
blood. Planner can be premium; critic stays cheap and mean.

## User surface

- Default skeptic model = economy hands / contract `call_class.skeptic`
- Optional bump skeptic one tier if objections ≥ N (default: never in v1)
- Cost purpose=`skeptic` already exists — keep attribution

## Acceptance (v1)

- [ ] Skeptic calls use cheap model under router policy
- [ ] Explicit SKEPTIC model env can pin
- [ ] Tests: skeptic slug ≠ premium brain when route=balanced

## v1 cuts

- No auto-escalate skeptic on findings (v1 always cheap)
