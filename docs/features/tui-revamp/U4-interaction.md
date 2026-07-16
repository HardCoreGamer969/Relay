# U4 — Interaction

**Stage:** U4 · **Status:** [MASTER](MASTER.md) only  
**Maps to:** REVAMP Stage T4  
**Depends on:** U2; U3 optional for approve-with-diff

## One-liner

Composer and permissions feel intentional; sessions are resumable from the UI.

## Work

1. **Multi-line composer** — `TextArea`-based; Enter submit, Shift+Enter newline;
   keep slash popover + history
2. **Approve modal** — command + policy reason + once / session / deny;
   embed U3 diff when present ([DECISIONS §6](DECISIONS.md))
3. **Session persistence + `/runs` picker** — serialize Session at run boundaries;
   select restores stream from transcript (builds on RunState when available)
4. **Context gauge** on rail (engine already resolves windows)
5. Slash `accepts_args` complete for config ops

## Acceptance

- [ ] Multi-line edit works; paste remains editable
- [ ] Approval never depends on typing `y` into the shared goal box
- [ ] `/runs` can restore a prior session stream (or clear “not available” if
      RunState blocked — note blocker in PROGRESS)
- [ ] Context % appears when known

## v1 cuts / blockers

- Full RunState may be a REVAMP prerequisite — if blocked, ship modal + composer
  first and leave resume as thin “view harness” until RunState lands
