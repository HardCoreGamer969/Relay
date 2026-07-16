# Relay Features Revamp — Master Plan

> Product differentiation roadmap (12 features + brand-defining **Model Router**).
>
> **Sibling, not child, of the engineering revamp:**
> [`../REVAMP.md`](../REVAMP.md) = quality, structure, parity hygiene
> (streaming, resume, native tool-calling, TUI split).
> **This folder** = what makes Relay feel unlike Claude Code / Codex / OpenCode.
>
> Do not merge the two roadmaps. Cross-link blockers only.

**Roadmap home for differentiation work.** Status lives in the table below.

---

## Doc rules (keep this system healthy)

| Rule | Detail |
|------|--------|
| Master is thin | This file = identity, **one** roadmap table, dependency sketch, non-goals. No feature designs here. |
| Feature files hold depth | `NN-<slug>.md` = problem, surface, hooks, acceptance, open questions. |
| Status lives once | Update the **Status** column below only. Feature docs do not carry a status field. |
| Ship note in the feature doc | When shipped, add `Shipped: vX.Y.Z` at the top of that feature file; flip Status here to `shipped`. |
| No third layer | Do not add `STATUS.md`, per-phase masters, or website mirrors until this table is genuinely painful. |
| REVAMP stays separate | If a feature needs infra (e.g. `RunState`), note it in **Blockers** and link REVAMP — do not copy REVAMP tasks here. |

Status values: `planned` · `designing` · `in progress` · `shipped` · `blocked` · `dropped`.

---

## Branching & commit rules

Home branch for this roadmap: **`cursor/features-revamp-af89`** (the features
revamp branch). All differentiation work eventually lands here.

| Rule | Detail |
|------|--------|
| Default land here | Design docs, feature slices, and fixes for this roadmap commit to `cursor/features-revamp-af89` and push often. |
| Sub-branch when useful | For a large or risky feature/slice, branch *from* the revamp branch (e.g. `cursor/a1-cost-envelope-af89`), implement there, then **merge back into** `cursor/features-revamp-af89`. Do not open a parallel long-lived roadmap branch. |
| Name pattern | Keep the `cursor/<descriptive-name>-af89` form for any sub-branch. |
| Commit as you go | Prefer small, descriptive commits (design lock, S1, S2, …) over one giant dump. Push before / after test passes on a slice. |
| One feature focus | Finish or clearly pause a feature (status + notes) before starting the next phase row, unless a blocker forces a detour. |
| Link the doc | PR / commit messages for implementation should reference `docs/features/NN-*.md` (and update MASTER status when status changes). |
| Don’t mix roadmaps | Engineering-only REVAMP work stays on its own track; if a features slice needs a REVAMP prerequisite, note the blocker here and land the infra where that work belongs, then resume the feature. |
| No drive-by refactors | Keep commits scoped to the active feature/slice; unrelated cleanup is out of scope unless it unblocks the slice. |

```text
main
  └── cursor/features-revamp-af89          ← roadmap home (docs + features)
        ├── cursor/a1-cost-envelope-af89   ← optional sub-branch → merge back
        └── cursor/<feature>-af89          ← optional sub-branch → merge back
```

---

## Identity

Most agents compete on one smart model that does everything. Relay competes on
**orchestration as the product**: narrow hands, every-model protocol, user-owned
judgment, honest budgets, and **model routing** (spend smart across roles).

Lead with bake-offs, profiles, plan forks, product firewall, protocol fitness,
and the router. Treat streaming / MCP / prettier markdown as REVAMP hygiene,
not the story.

---

## Roadmap (build order)

Work top to bottom. Skip a row only with a written reason in **Notes**.

| Phase | # | Feature | Doc | Status | Blockers / notes |
|-------|---|----------|-----|--------|------------------|
| A1 | 7 | Cost Envelope Contracts | [07](07-cost-envelope.md) | shipped | CostEnvelope + scope/warn + CLI/TUI/solo/runlog |
| A2 | 12 | Explain the Harness (`/why`) | [12](12-explain-harness.md) | shipped | `explain_events` + `/why` + `runs --explain` |
| A3 | 9 | Finding-Driven Memory | [09](09-finding-memory.md) | shipped | `.relay/memory.json` + pin/forget |
| B1 | 2 | Assumption Profiles | [02](02-assumption-profiles.md) | shipped | builtins + `--profile` / TUI `/profile` |
| B2 | 5 | Product-Decision Firewall | [05](05-product-firewall.md) | shipped | class= + dial×class matrix; unlabeled→product |
| B3 | 4 | Hands Context Dial | [04](04-context-dial.md) | shipped | needle/findings/summary/wide; never brain pool |
| B4 | **13** | **Model Router** (brand) | [13](13-model-router.md) | shipped | economy/balanced/premium + replan bump/freeze |
| C1 | 1 | Model Bake-Off (`relay duel`) | [01](01-bake-off.md) | planned | Needs A1 receipts; `provider` on CallRecord → [REVAMP §4](../REVAMP.md) |
| C2 | 8 | Protocol Fitness Lab | [08](08-protocol-lab.md) | planned | Pairs with C1 |
| D1 | 6 | Adversarial Reviewer | [06](06-adversarial-reviewer.md) | planned | Investigation loop exists; reviewer fail-open → REVAMP |
| D2 | 3 | Plan Fork / Time-Travel | [03](03-plan-fork.md) | planned | Needs `RunState` / resume → [REVAMP Phase 2](../REVAMP.md) |
| D3 | 11 | Diff-as-Interface | [11](11-diff-interface.md) | planned | Stronger with D2 checkpoints |
| D4 | 10 | Orchestra Mode | [10](10-orchestra.md) | planned | Needs A3, B2, stable step boundaries |

**Out of roadmap (intentionally skipped):** local-first remote swarm, teaching/ghost-hands, standalone constraint cards (fold into #5 / #9 if needed).

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
                    D1 Adversarial → D2 Plan fork → D3 Diff interface → D4 Orchestra
```

---

## Non-goals

- Cloning Claude Code / Codex / OpenCode checklists as the roadmap
- A Relay cloud/SaaS control plane
- Replacing the text-protocol moat with native-only tool-calling (native FC
  *with* tag fallback may land via REVAMP — not instead of the protocol)
- Teaching mode / remote swarm (interesting; not this folder)

---

## Day-to-day

1. Take the first non-`shipped` / non-`dropped` row in phase order.
2. Open its `NN-*.md`; tighten acceptance criteria before coding.
3. Follow **Branching & commit rules** above (revamp branch by default; sub-branch only if needed).
4. Flip **Status** in the table above as you move `designing` → `in progress` → `shipped`; on ship, add `Shipped: vX.Y.Z` to the feature doc.
5. New REVAMP dependency → one line in **Blockers / notes**, not a new doc.
