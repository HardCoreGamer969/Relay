# Relay TUI Revamp — Master Plan

> Full cockpit redesign: **IDE density**, website brand (red/black + SVG mark),
> instrument-panel motion with a hard off switch. Sibling to
> [`../MASTER.md`](../MASTER.md) (product features) and engineering
> [`../../REVAMP.md`](../../REVAMP.md) Stages T1–T5 (folded into U0–U6 below).
>
> **Progress:** [`PROGRESS.md`](PROGRESS.md) · **Locked decisions:** [`DECISIONS.md`](DECISIONS.md)

**Branch home:** `cursor/features-revamp-af89` (same rules as parent MASTER).

---

## Doc rules

| Rule | Detail |
|------|--------|
| This MASTER is thin | Identity, roadmap table, layout sketch, non-goals. |
| Stage files hold depth | `U0`–`U6` specs = surfaces, hooks, acceptance, cuts. |
| Status lives once | Update **Status** below only. |
| PROGRESS is the journal | Append after each stage review gate. |
| Bridge is sacred | Do not rewrite `relay/bridge.py` contracts; build against them. |

Status: `planned` · `designing` · `in progress` · `shipped` · `blocked` · `dropped`.

---

## Identity

The TUI is a **spend-aware conductor console**, not a chat skin.

You always see: phase · plan (with active highlight) · step · cost/remaining ·
route · context · queue — without digging through `/` commands.

Motion is **instrument feedback** (phase change, step commit, cost warn,
route tick). Ambient flair is optional; **`RELAY_TUI_ANIM=0` / `/anim off`
kills every animation**.

Brand matches the website: pure black field, signal red accent, SVG Relay mark.

---

## Locked answers (summary)

| # | Topic | Decision |
|---|--------|----------|
| 1 | Density | **IDE cockpit** (fixed chrome + stream) |
| 2 | Plan | **Full plan dock by default**, highlight active step; modes: `full` / `active` / `hidden` |
| 3 | Cost | **First-class** (same weight as step); warn thresholds escalate style |
| 4 | Route | **Always show** compact `route=…`; pulse on `route_change` |
| 5 | Motion | Instrument panel; **global anim kill switch** |
| 6 | Approve | **Dedicated modal** (command + reason + once/session/deny; diff when present) |
| 7 | Scope | **Full U0–U6** |
| 8 | Brand | Website palette + `website/assets/logo*.svg` |

Full write-up: [`DECISIONS.md`](DECISIONS.md).

---

## Layout sketch (cockpit)

```text
┌─ status rail ─────────────────────────────────────────────────────────────┐
│ ● EXECUTING · step 2/5 · $0.12 / $1.00 left · route=balanced · ctx 34%   │
│   brain …sonnet · hands …haiku · queued:1 · esc interrupt                 │
├─ plan dock (default: full) ──────────────┬─ stream ───────────────────────┤
│ ◉ 1. scaffold module                     │ [brain] plan committed         │
│ ▸ 2. wire CLI   ← active                 │ [hands] write path=…           │
│ ○ 3. tests                               │ [finding] …                    │
│                                          │ …                              │
├──────────────────────────────────────────┴────────────────────────────────┤
│ composer (multi-line) · / slash · state-aware placeholder                 │
└───────────────────────────────────────────────────────────────────────────┘
```

Narrow terminals: plan dock collapses to `active` mode automatically
(override via `/plan full`).

---

## Roadmap (build order)

| Stage | Name | Doc | Status | Notes |
|-------|------|-----|--------|-------|
| U0 | Package split | [U0-package-split.md](U0-package-split.md) | planned | REVAMP T1; no visual change |
| U1 | Foundation | [U1-foundation.md](U1-foundation.md) | planned | Off-thread I/O, virtualized stream |
| U2 | Cockpit chrome | [U2-cockpit.md](U2-cockpit.md) | planned | Status rail + plan dock + gauges |
| U3 | Rich stream | [U3-rich-stream.md](U3-rich-stream.md) | planned | Markdown, diffs, folds |
| U4 | Interaction | [U4-interaction.md](U4-interaction.md) | planned | Composer, approve modal, resume |
| U5 | Motion system | [U5-motion.md](U5-motion.md) | planned | Instrument motions + kill switch |
| U6 | Polish | [U6-polish.md](U6-polish.md) | planned | Theme polish, `/find`, logo welcome |

```text
U0 split ──► U1 foundation ──► U2 cockpit ──► U3 rich stream
                                  │                │
                                  ▼                ▼
                             U5 motion ◄──── U4 interaction ──► U6 polish
```

U2 is the **functional** leap. U5 depends on U2 event hooks. U3/U4 can
overlap after U2 if staffing allows; default is serial for one agent.

---

## Brand tokens (from `website/css/style.css`)

| Token | Value | TUI use |
|-------|-------|---------|
| `--bg-deep` / `--bg` | `#000000` / `#050505` | Screen / panels |
| `--bg-raised` / `--bg-card` | `#0a0a0a` / `#0f0f0f` | Plan dock, dialogs |
| `--red` / `--red-bright` | `#ff0000` / `#ff1a1a` | Brand, brain, active LED |
| `--warn` | `#ff6600` | Envelope warns, cost pulse escalate |
| `--text` / `--text-dim` / `--text-muted` | `#f0f0f0` / `#888` / `#555` | Primary / hands / chrome |
| `--border` | `#1a1a1a` | Rules, dock edges |
| Logo | `website/assets/logo-icon.svg` + `logo.svg` | Welcome + about; package under `relay/assets/` |

**Role remap vs today:** brain → red (was magenta); hands → light/dim white (was cyan);
findings → warn amber or soft red-tint green reserved for success only.
Keep accessibility contrast; no website scanlines/particles in the TUI.

---

## Non-goals

- Rewriting the agent loop / `bridge.py` semantics
- Competing with GUI IDEs (no mouse-only workflows)
- Ambient particles, CRT scanlines, perpetual idle animation
- Spending model tokens to “pretty-print” the UI
- Shipping U3–U6 before U0–U2 (structure before spectacle)

---

## Day-to-day

1. Next non-shipped stage in order.
2. Thin vertical slice; hermetic TUI tests; PROGRESS review gate.
3. Commit + push; flip Status; add `Shipped:` on stage doc.
4. Only then start the next stage.
