# U3 — Rich Stream

**Stage:** U3 · **Status:** [MASTER](MASTER.md) only  
**Maps to:** REVAMP Stage T3  
**Depends on:** U1 (virtualized stream), U2 (cockpit stable)

## One-liner

Make model and tool output **readable**: Markdown brain turns, syntax
diffs/previews, collapsible tool bodies — still zero new tokens.

## Work

1. Markdown (or Rich Markdown renderables) for brain / conversation turns
2. Syntax-highlighted diffs / code observations for write tools
3. Collapsible tool output (summary line → expand); pairs with U1 caps
4. Theme object fully owns stream styles (website tokens)

## Acceptance

- [ ] Code fences and lists in brain text render as structured blocks
- [ ] Edit/patch observations show a diff-like view when content is diff-shaped
- [ ] Tool lines fold by default; key/click expands
- [ ] `/log` export remains plain/redacted (not dependent on widget internals)

## v1 cuts

- No click-to-open-in-editor yet (U6)
- No live token streaming until engine streaming lands (then status LED split)
