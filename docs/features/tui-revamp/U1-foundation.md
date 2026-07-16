# U1 — Foundation

**Stage:** U1 · **Status:** [MASTER](MASTER.md) only  
**Maps to:** REVAMP Stage T2  
**Depends on:** U0

## One-liner

Correctness + felt latency: nothing blocks the UI thread; the stream stays
readable and bounded on long runs.

## Work

1. **Off-thread I/O** — setup model lists, `/model` fetch, `/doctor` probes via
   Textual `@work(thread=True)` + spinner states (injectable seams already exist).
2. **Virtualized / capped stream** — replace unbounded `Static` rows with
   `RichLog` (`max_lines`) or custom line widget; cap mirrors; **don’t yank
   scroll** unless user was already at bottom.
3. **Harden marshal** — log exceptions in `_marshal`; prefer events over 0.2s
   transcript poll; snapshot/lock reads of ledger/transcript from UI thread.
4. **Unified slash args** — `Command.accepts_args`; `/model …` works like `/queue …`.
5. **Kill string round-trips** — structured stream entries; verdict from payload.

## Acceptance

- [ ] No live HTTP during `compose()` on the UI thread
- [ ] Long-run stream memory bounded; scrollback readable mid-run
- [ ] Marshal errors visible in debug / activity, not silently dropped
- [ ] Existing TUI tests green; add cases for scroll-pin and worker dialogs

## v1 cuts

- Full render-model layer can wait for U6; mirrors remain for `/log` for now
