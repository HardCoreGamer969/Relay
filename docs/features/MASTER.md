# Relay Features Revamp — Master Plan

> Differentiation roadmap: the 12 product features plus the brand-defining
> **Model Router** (#13). This is *not* the engineering refactor in
> [`../REVAMP.md`](../REVAMP.md) — that doc covers quality, structure, and
> parity hygiene (streaming, resume, native tool-calling). **This** doc is
> what makes Relay feel unlike Claude Code, Codex, or OpenCode.

**Status:** planning only — no implementation yet.
**Branch intent:** `features-revamp` (git: `cursor/features-revamp-af89`).

---

## On this doc layout

| File | Job |
|------|-----|
| `MASTER.md` (this file) | Big picture, identity, phased order, dependency graph, progress table |
| `NN-<slug>.md` | One feature: problem, surface, hooks, acceptance, open questions |

**Why this shape works:** a single mega-doc becomes unreadable once designs grow;
thirteen orphan docs with no index lose the order and dependencies. Master =
navigation + sequencing; feature files = depth. Keep the master thin: do **not**
duplicate full designs here — link out and update status in the table below.

**Optional later (not created yet):** a `STATUS.md` only if the table in this
file gets noisy; a mermaid export for the website. Avoid a third layer until
you feel pain.

**Relation to `REVAMP.md`:** ship parity items from REVAMP when a feature
needs them (e.g. durable `RunState` before plan fork). Do not block the whole
differentiation roadmap on finishing REVAMP Phase 3.

---

## Identity wedge

Most agents compete on *one smart model that does everything*. Relay competes
on **orchestration as the product**:

- Brain / hands with **narrow executor context**
- **Every-model** text protocol (OpenRouter-first)
- User-owned **judgment** (assumption dial → profiles → product firewall)
- **Honest budgets** (steps, cost, escalations) with receipts
- **Model routing** — spend smart across roles mid-run, not “pick one chat model”

Lead marketing with bake-offs, assumption profiles, plan forks, the
product-decision firewall, protocol fitness, and the router. Treat streaming /
MCP / prettier markdown as hygiene, not the story.

---

## Feature index

| # | Feature | Doc | Role |
|---|---------|-----|------|
| 1 | Model Bake-Off (`relay duel`) | [01-bake-off.md](01-bake-off.md) | Proof of the architecture |
| 2 | Assumption Profiles | [02-assumption-profiles.md](02-assumption-profiles.md) | Judgment as product |
| 3 | Plan Time-Travel / Fork Studio | [03-plan-fork.md](03-plan-fork.md) | Git for intent |
| 4 | Hands Context Dial | [04-context-dial.md](04-context-dial.md) | Amnesia as a feature |
| 5 | Product-Decision Firewall | [05-product-firewall.md](05-product-firewall.md) | Refuse invented product calls |
| 6 | Adversarial Reviewer | [06-adversarial-reviewer.md](06-adversarial-reviewer.md) | Planner + skeptic |
| 7 | Cost Envelope Contracts | [07-cost-envelope.md](07-cost-envelope.md) | Budgets with receipts |
| 8 | Protocol Fitness Lab | [08-protocol-lab.md](08-protocol-lab.md) | Every-model moat, measured |
| 9 | Finding-Driven Memory | [09-finding-memory.md](09-finding-memory.md) | Curated decisions, not chat sludge |
| 10 | Orchestra Mode | [10-orchestra.md](10-orchestra.md) | One brain, many narrow hands |
| 11 | Diff-as-Interface | [11-diff-interface.md](11-diff-interface.md) | Step-scoped accept / rewind |
| 12 | Explain the Harness (`/why`) | [12-explain-harness.md](12-explain-harness.md) | Debuggable autonomy |
| **13** | **Model Router** | [13-model-router.md](13-model-router.md) | **Brand-defining** |

Skipped from the creative list (by choice): local-first swarm without cloud,
teaching/ghost-hands mode, constraint cards as a standalone feature (fold
constraint cards into #5 / #9 when useful).

---

## Recommended build order

Order optimizes for: (1) leverage what already exists, (2) unlock later
features, (3) ship visible identity early without boiling the ocean.

### Phase A — Honest foundation (ship first)

Small surface area; amplifies existing telemetry, budgets, and memory Stage 1.

| Order | # | Feature | Why here |
|------:|---|----------|----------|
| A1 | 7 | Cost Envelope Contracts | Extends `--max-cost` / step ceilings into contracts + receipts |
| A2 | 12 | Explain the Harness | Flight recorder; makes every later feature debuggable |
| A3 | 9 | Finding-Driven Memory | Persist shared pool across runs; feeds firewall, orchestra, router |

### Phase B — Judgment + brand (identity)

| Order | # | Feature | Why here |
|------:|---|----------|----------|
| B1 | 2 | Assumption Profiles | Productize the existing 1–5 dial |
| B2 | 5 | Product-Decision Firewall | Typed escalations; profiles become meaningful |
| B3 | 4 | Hands Context Dial | Expose narrowness; telemetry for “amnesia wins” |
| B4 | **13** | **Model Router** | Brand wedge; needs cost honesty + escalation types |

### Phase C — Proof (show the world)

| Order | # | Feature | Why here |
|------:|---|----------|----------|
| C1 | 1 | Model Bake-Off | Needs solid cost/role telemetry from A/B |
| C2 | 8 | Protocol Fitness Lab | Public “Relay fitness” for OpenRouter slugs; pairs with duel |

### Phase D — Deep orchestration (harder infrastructure)

| Order | # | Feature | Why here |
|------:|---|----------|----------|
| D1 | 6 | Adversarial Reviewer | Extends investigation loop; optional second brain |
| D2 | 3 | Plan Fork / Time-Travel | Needs checkpoints / resume-shaped `RunState` |
| D3 | 11 | Diff-as-Interface | Step-scoped diffs + rewind on stable step boundaries |
| D4 | 10 | Orchestra Mode | Hardest; needs memory, firewall, conflict detection |

```text
A1 Cost envelope ──┐
A2 /why            ├──► B1 Profiles ──► B2 Firewall ──┐
A3 Finding memory ─┘         │                        │
                             ▼                        ▼
                        B3 Context dial ──► B4 Model Router (brand)
                                                   │
                                    ┌──────────────┴──────────────┐
                                    ▼                             ▼
                               C1 Bake-off                   C2 Protocol lab
                                    │
                    D1 Adversarial reviewer
                    D2 Plan fork
                    D3 Diff-as-interface
                    D4 Orchestra
```

---

## Progress tracker

Update status as work starts. Values: `planned` · `designing` · `in progress` · `shipped` · `dropped`.

| Phase | # | Feature | Status | Notes |
|-------|---|----------|--------|-------|
| A1 | 7 | Cost Envelope | planned | Partial: `--max-cost`, step ceilings exist |
| A2 | 12 | Explain Harness | planned | Events exist; no `/why` aggregation yet |
| A3 | 9 | Finding Memory | planned | Stage 1 pools exist; persist + Stage 2 read TBD |
| B1 | 2 | Assumption Profiles | planned | Dial exists; named profiles do not |
| B2 | 5 | Product Firewall | planned | Escalations exist; no typed taxonomy |
| B3 | 4 | Context Dial | planned | Narrow hands hardcoded today |
| B4 | 13 | Model Router | planned | Static role→model binding today |
| C1 | 1 | Bake-Off | planned | Per-role telemetry seed exists |
| C2 | 8 | Protocol Lab | planned | — |
| D1 | 6 | Adversarial Reviewer | planned | Investigation/reviewer primitive exists |
| D2 | 3 | Plan Fork | planned | Needs RunState/checkpoints (REVAMP) |
| D3 | 11 | Diff Interface | planned | No-op honesty / step boundaries help |
| D4 | 10 | Orchestra | planned | — |

---

## Non-goals for this revamp

- Cloning Claude Code / Codex / OpenCode feature checklists as the roadmap
- Building a Relay cloud/SaaS control plane
- Replacing the text-protocol moat with native-only tool-calling (native FC
  may be added *with* tag fallback per REVAMP — not instead of it)
- Teaching/ghost-hands and remote local-swarm (interesting; out of scope here)

---

## How to use these docs day-to-day

1. Pick the next `planned` row in Phase order (do not skip A→B→C without a reason).
2. Open that feature’s `NN-*.md`; refine design until acceptance criteria are crisp.
3. Implement on a focused PR; link the feature doc in the PR body.
4. Flip status in this table; note ship version in the feature doc.
5. If a feature needs REVAMP infrastructure, add a one-line blocker note here
   and a cross-link — do not fork a second roadmap.

---

## Source

Proposals distilled from product research against Relay’s existing DNA
(brain/hands, narrow context, assumption dial, text protocol, bake-off
telemetry seed, bounded autonomy). Creative alternatives not selected for
this roadmap are listed above under “Skipped.”
