# Relay Router v2 — Master Plan

> Phase E of the differentiation roadmap: deepen the brand of **Relay as a
> smart model router, not a chat app**. Sibling to [`../MASTER.md`](../MASTER.md)
> (A1–D4 shipped). Do not merge into the engineering [`../../REVAMP.md`](../../REVAMP.md).

**Branch home:** `cursor/features-revamp-af89` (same rules as parent MASTER).  
**Progress log:** [`PROGRESS.md`](PROGRESS.md)

---

## Doc rules

| Rule | Detail |
|------|--------|
| This MASTER is thin | Identity, one roadmap table, dependency sketch. No full designs here. |
| Feature files hold depth | `NN-<slug>.md` = problem, surface, hooks, acceptance, v1 cuts. |
| Status lives once | Update **Status** in the table below only. |
| Progress.md is the journal | Append dated notes per feature review gate (what shipped, what deferred). |
| No third layer beyond this | Do not add per-phase masters. |
| Review gate | Do not start feature N+1 until N is tested, reviewed, committed, and logged in PROGRESS. |

Status values: `planned` · `designing` · `in progress` · `shipped` · `blocked` · `dropped`.

---

## Identity

OpenRouter / NotDiamond route **per prompt**. Relay routes **per role, purpose,
phase, and harness event** — with envelopes, receipts, and `/why spend`.

Lead with: call-class policy, fitness-gated hands, counterfactual receipts,
repo-learned routes. Treat provider `:floor` as a sub-layer under Relay’s policy.

---

## Roadmap (build order)

| Phase | # | Feature | Doc | Status | Notes |
|-------|---|----------|-----|--------|-------|
| E1 | 8 | Route contracts | [08-route-contracts.md](08-route-contracts.md) | shipped | Foundation policy artifact |
| E2 | 1 | Call-class routing | [01-call-class-routing.md](01-call-class-routing.md) | shipped | Needs E1 |
| E3 | 3 | Envelope-as-UX | [03-envelope-ux.md](03-envelope-ux.md) | shipped | Broker status line |
| E4 | 4 | Counterfactual receipt | [04-counterfactual-receipt.md](04-counterfactual-receipt.md) | shipped | $ saved vs premium |
| E5 | 9 | Explain the spend | [09-explain-spend.md](09-explain-spend.md) | shipped | Extends `/why` |
| E6 | 6 | Cheap skeptic assassin | [06-cheap-skeptic.md](06-cheap-skeptic.md) | shipped | Fixed cheap skeptic model |
| E7 | 7 | Provider micro-routing | [07-provider-micro.md](07-provider-micro.md) | shipped | `:floor` / max_price under policy |
| E8 | 11 | Phase-aware routes | [11-phase-aware.md](11-phase-aware.md) | shipped | Plan vs execute vs review |
| E9 | 2 | Fitness-gated hands | [02-fitness-gated-hands.md](02-fitness-gated-hands.md) | shipped | Uses probe fitness |
| E10 | 12 | Orchestra × router | [12-orchestra-router.md](12-orchestra-router.md) | shipped | Cheap hands, hot conductor |
| E11 | 5 | Repo-learned route | [05-repo-learned-route.md](05-repo-learned-route.md) | shipped | From duels/runlog |
| E12 | 10 | Shadow routing | [10-shadow-routing.md](10-shadow-routing.md) | shipped | Opt-in learn mode |

```text
E1 Route contracts ──► E2 Call-class ──► E8 Phase-aware ──┐
         │                    │                           │
         ▼                    ▼                           ▼
    E3 Envelope UX      E9 Fitness hands          E10 Orchestra×router
         │                    │
         ▼                    ▼
    E4 Counterfactual ◄── E5 Explain spend
         │
         ▼
    E6 Cheap skeptic · E7 Provider micro
         │
         ▼
    E11 Repo-learned ──► E12 Shadow routing
```

---

## Non-goals

- Competing with `openrouter/auto` as a prompt classifier
- Silent model swaps without `route_change` / spend explain
- Chat-session model picker UX
- Unbounded dual-calling (shadow stays opt-in + budgeted)

---

## Day-to-day

1. Take next non-shipped row in phase order.
2. Implement thin v1; hermetic tests; review checklist in PROGRESS.
3. Commit + push; flip Status here; add `Shipped:` on feature doc.
4. Only then start the next row.
