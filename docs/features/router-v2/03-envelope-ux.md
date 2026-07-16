# 03 — Envelope-as-UX

**Phase:** E3 · **Status:** [MASTER](MASTER.md) only  
**Depends on:** A1 envelope; B4 route

## One-liner

Make the status line and preflight read like a spend broker, not a chat header.

## User surface

- Preflight / status: `route=… · brain a→b (reason) · freeze@80% · remaining $…`
- TUI `/route` cockpit (inspect + session pin); `/model` labeled as override
- CLI run panel includes broker line

## Acceptance (v1)

- [ ] Broker string helper used by CLI preflight + TUI status
- [ ] `/route` shows active route, pins, freeze state
- [ ] Tests for broker string formatting

## v1 cuts

- No animated sparkline; text-first
