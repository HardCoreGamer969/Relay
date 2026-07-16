# TUI Revamp — Locked Decisions

Answers from product + recommendations where asked. Do not re-litigate without
an explicit reopen; note corrections as dated addenda at the bottom.

---

## 1. Density — IDE cockpit

Fixed chrome always present during a run:

- **Status rail** (top): phase, step, cost, route, context, models, queue, hints
- **Plan dock** (left or top band): plan visibility per §2
- **Stream** (main): chronological truth
- **Composer** (bottom): multi-line + slash

Welcome remains a brand-first screen; first submit transitions into cockpit.

---

## 2. Plan dock — full by default, configurable

| Mode | Behavior |
|------|----------|
| `full` (**default**) | Entire plan listed; **active step highlighted** (▸ / bright); done/failed icons |
| `active` | Only current + prev/next one-liner (narrow / focus) |
| `hidden` | Dock collapsed; active step remains on status rail |

Surfaces: `/plan full|active|hidden`, env `RELAY_TUI_PLAN=full|active|hidden`,
session-only until we add config persistence in U6.

Auto: if terminal width < ~100 cols, coerce `full` → `active` unless user pinned `full`.

---

## 3. Cost prominence — **first-class** (recommendation)

**Rec:** Cost / remaining is **as loud as step progress** on the status rail.

| State | Presentation |
|-------|----------------|
| No envelope | `$0.12` this-goal (and session total on `/cost`) |
| Envelope set | `$0.12 / $1.00 left` always |
| Warn 50/80/90/99% | Same slot; style escalates (dim → amber/`--warn` → bold flash once per threshold) |
| Hit ceiling | Phase reads STOPPED; rail stays red/warn until dismissed |

Rationale: Relay’s brand is the spend broker. Hiding cost until warn trains users
to ignore the one differentiator chat apps don’t have.

---

## 4. Route visibility — **always compact** (recommendation)

**Rec:** Always show `route=balanced` (or contract name) on the status rail.

| Event | Presentation |
|-------|----------------|
| Steady | Dim compact `route=…` |
| `route_change` | Brief highlight / tick (if anim on); detail still in `/route` and `/why` |
| Freeze | `freeze*` marker next to route |

Rationale: call-class / phase / fitness bumps are meaningless if the user can’t
see which policy is live. Always-on compact text is cheap; pulsing only on change
avoids noise.

---

## 5. Motion — instrument panel + kill switch

Default aesthetic: **instrument panel** (phase crossfade, step commit, cost warn
flash, route tick, tool-fold expand). Keep a short welcome→working transition;
retire perpetual datamosh loops as the default long-running motion.

**Kill switch (required):**

| Surface | Effect |
|---------|--------|
| `RELAY_TUI_ANIM=0` / `false` / `off` | All animations off (boot instant, no LED breathe, no pulses) |
| `/anim off` · `/anim on` | Session override (not written to config unless U6 adds persist) |
| Config `tui.animations: false` | Durable default (U6) |

When off: state changes are instant text updates only. Tests default to off
where timing-sensitive.

---

## 6. Approve UX — dedicated modal (recommendation)

Replace y/n-through-composer with a **modal**:

1. Command verbatim (folded if huge)
2. Policy reason (`CONFIRM` / why)
3. Actions: **[1] once · [2] session · [3] deny** (keys + click)
4. When a step diff exists (D3 / U3): show diff pane in the same modal

Bridge unchanged: modal calls `request.deliver("yes"|"no")`; session allowlist
is UI-side state passed into `run_kwargs`.

---

## 7. Scope — full U0–U6

Ship the whole stack on `cursor/features-revamp-af89`, stage-gated via PROGRESS.
Do not skip U0/U1 to chase visuals.

---

## 8. Brand — website palette + SVG logo

Source of truth: `website/css/style.css` `:root` and `website/assets/logo.svg` /
`logo-icon.svg`.

TUI theme object copies those tokens (see MASTER brand table). Welcome hero uses
the **SVG mark** (package copy under `relay/assets/`), not the old block-glyph
wordmark as the primary brand — letterspaced `RELAY` text may remain as fallback
when SVG cannot render.

Brain/hands colors follow the site (brain=red, hands=light/dim), not the old
magenta/cyan pair — document the break in U2/U6 release notes.

---

## Addenda

_(none yet)_
