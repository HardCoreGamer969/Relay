# 05 — Product-Decision Firewall

**Phase:** B2 · **Status:** planned · **Depends on:** B1 profiles (can stub taxonomy first); A3 memory for pinned decisions

[← Master plan](MASTER.md)

---

## One-liner

Typed escalations so **product** decisions are never auto-answered; tech/mechanical classes may proceed under the assumption dial.

## Why it sets Relay apart

Agents either nag constantly or silently invent UX/API choices. Relay already stops on `unresolved_escalation`. Make “we refuse to invent product decisions” a visible brand promise.

## Taxonomy (v1)

| Class | Examples | Auto? |
|-------|----------|-------|
| `product` | UX copy, public API shape, naming users see | Never |
| `tech` | Library choice, refactor scope | Dial-dependent |
| `mechanical` | Lint, import path, formatting | Usually yes |

Optional later: **constraint cards** (`no new deps`, `API stable`) as pinned shared-memory directives that hands refusals can cite.

## User surface

- Brain must label `<question class="product|tech|mechanical">` (or equivalent protocol)
- TUI: sticky **decision inbox**; unanswered `product` pauses cleanly
- `/assume` interacts with tech/mechanical only — never product
- Run status remains honest when blocked on product input

## Hooks into existing code

- Escalation / question protocol tags
- Assumption dial enforcement sites
- Bridge `UiRequest` ask path
- Shared memory directives (A3)

## Acceptance criteria

- [ ] Unlabeled questions fail closed (treat as `product` or reject) — pick one, document it
- [ ] `product` questions never auto-answered regardless of dial/profile
- [ ] Decision inbox lists open product questions with step id
- [ ] Tests cover dial × class matrix

## Open questions

- Fail closed as `product` vs hard protocol error for missing class?
- Can the adversarial reviewer (#6) force a reclassification?

## Out of scope (v1)

- Multi-user approval workflows / Slack-style decision routing
