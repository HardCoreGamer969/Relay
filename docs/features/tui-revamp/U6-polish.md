# U6 — Polish & Brand Finish

**Stage:** U6 · **Status:** [MASTER](MASTER.md) only  
**Depends on:** U2–U5

## One-liner

Website-faithful brand finish, discoverability, and durable prefs.

## Work

1. **SVG logo on welcome** — ship `relay/assets/logo-icon.svg` (+ full mark);
   render on welcome / about; ASCII/`RELAY` text fallback if SVG path fails
2. **Theme complete** — one Textual CSS variable sheet from website tokens;
   remove leftover `#06090e` / magenta/cyan literals
3. **Durable prefs** — `tui.animations`, `tui.plan_dock` in config
4. **`/find`** scrollback search; copy visible region
5. **Clickable paths** in tool lines (open-in-editor when `$EDITOR` / OSC 8)
6. **Render-model** for stream entries shared by widget + `/log` tests

## Acceptance

- [ ] Welcome shows SVG mark (or documented fallback) matching site identity
- [ ] Palette matches MASTER brand table
- [ ] Prefs survive restart
- [ ] `/find` locates a known stream string in tests

## v1 cuts

- Full user theme marketplace / arbitrary CSS injection — out of scope
- Website scanlines/particles — never in TUI
