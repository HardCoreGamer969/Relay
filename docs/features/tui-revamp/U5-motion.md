# U5 — Motion System

**Stage:** U5 · **Status:** [MASTER](MASTER.md) only  
**Depends on:** U2 (chrome hooks); U0 theme

## One-liner

Instrument-panel motion tied to harness events, with a **hard global off**.

## Motions (default on)

| Motion | Trigger |
|--------|---------|
| Welcome → cockpit transition | First goal submit |
| Phase crossfade on rail | InputRouter / engine phase change |
| Plan step commit | Step becomes active / done / failed |
| Cost warn flash | Envelope threshold fire (once) |
| Route tick | `route_change` event |
| Tool fold expand | User expand (tiny) |

No idle particles. No CRT. No long boot loop (keep short decode optional).

## Kill switch

- `RELAY_TUI_ANIM=0|false|off`
- `/anim off` · `/anim on` (session)
- Config `tui.animations` in U6

When off: instant text updates only; tests prefer off.

## Acceptance

- [ ] Each listed motion fires only on its event
- [ ] Kill switch disables **all** timers/animations including LED breathe
- [ ] Hermetic tests with anim off (no timing flakes)
- [ ] With anim on, short durations (sub-second) — never block input

## v1 cuts

- No user-custom motion profiles beyond on/off
