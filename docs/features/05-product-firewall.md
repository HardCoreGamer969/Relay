# 05 — Product-Decision Firewall

**Phase:** B2 · **Status:** [MASTER roadmap table](MASTER.md) only  
**Shipped:** features-revamp (`relay/firewall.py` + protocol `class=` + dial×class matrix)  
**Depends on:** B1 profiles (taxonomy can stub first); A3 memory for pinned decisions

## Blockers

- None hard; protocol tag extensions stay in-repo

---

## One-liner

Typed escalations so **product** decisions are never auto-answered; tech/mechanical classes may proceed under the assumption dial.

## Why it sets Relay apart

Agents either nag constantly or silently invent UX/API choices. Relay already stops on `unresolved_escalation`. Make “we refuse to invent product decisions” a visible brand promise.

## Taxonomy (v1)

| Class | Examples | Auto? |
|-------|----------|-------|
| `product` | UX copy, public API shape, naming users see | Never |
| `tech` | Library choice, refactor scope | Dial 1–2 only |
| `mechanical` | Lint, import path, formatting | Usually yes (not dial 5) |

**Fail closed:** unlabeled questions are treated as `product` (not a hard protocol error).

Optional later: **constraint cards** (`no new deps`, `API stable`) as pinned shared-memory directives that hands refusals can cite.

## User surface

- Brain/hands label `<question class="product|tech|mechanical">` (or leading `[tech]` / `class: tech` in the body)
- Harness `/why`: open questions list class + step id (decision inbox v1)
- `/assume` interacts with tech/mechanical only — never product
- Run status remains honest when blocked on product input

## Hooks into existing code

- Escalation / question protocol tags (`relay/protocol.py`)
- Assumption dial enforcement in `answer_or_escalate` (`relay/planner.py` + `relay/firewall.py`)
- Bridge `UiRequest` ask path
- Shared memory directives (A3)

## Acceptance criteria

- [x] Unlabeled questions fail closed (treat as `product`) — documented here
- [x] `product` questions never auto-answered regardless of dial/profile
- [x] Decision inbox lists open product questions with step id (harness open_questions)
- [x] Tests cover dial × class matrix

## Open questions

- Fail closed as `product` vs hard protocol error for missing class? **v1: treat as product.**
- Can the adversarial reviewer (#6) force a reclassification?

## Out of scope (v1)

- Multi-user approval workflows / Slack-style decision routing
- Sticky TUI decision-inbox pane (harness listing is the v1 surface)
