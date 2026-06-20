# Relay

> A coding agent built on a **planner/executor** architecture — a "brain" model
> that plans and a "hands" model that executes — with every model reached
> through **OpenRouter**.

**Website:** [hardcoregamer969.github.io/Relay](https://hardcoregamer969.github.io/Relay/)

## The brain / hands idea

Relay separates *thinking* from *doing*. The **brain** (planner) decides what
should happen next; the **hands** (executor) carry it out. Each role is bound to
a model, and both roles are reached through a single seam — `call_model(role, …)`
— so the system never cares *which* model sits behind a role. Everything runs
through [OpenRouter](https://openrouter.ai), which is itself the model-agnostic
layer: any OpenRouter model slug works for either role, and swapping a model is a
config/env change, never a code change.

## Architecture (brain + hands)

This is the milestone Relay is named for. `relay run` drives **two** models:

- The **brain** (planner) reads the goal plus a shallow **project digest** and,
  optionally after a few read-only `read`/`list`/`grep` investigations, emits an
  ordered `<plan>` of concrete, executor-sized `<step>`s. The brain is
  **read-only** — it cannot `edit` or `bash` (those attempts are refused).
- The **hands** (executor) carry out each step **one at a time, in a narrow
  context**: the current step instruction plus a one-line carry-over of what
  earlier steps produced — *not* the full plan, *not* the brain's reasoning,
  *not* prior steps' raw transcripts. That narrowness is the point: it is both
  cheaper and often higher-quality (less to get distracted by).

**Bounded interleaving.** The brain plans once up front and re-engages **only on
escalation** — it does not review every successful step. A step *fails* when the
executor emits `<blocked>`, exhausts its per-step budget, or can't follow the
protocol; the harness then asks the brain to **replan** the remaining tail
(keeping completed steps) or `<abort>`. Every loop is bounded
(`max_executor_steps=12` per step, `max_escalations=3` replans, and a global
step ceiling `max_total_steps` — **on by default at 50**, raisable/disable-able)
so a weak model can't burn money in a spiral. If a replan re-issues a step that
already dead-ended the same way, Relay recognizes the loop and **stops fast**
rather than grinding the budget.

A planned run ends in one clear terminal status: `completed`, `planning_failed`,
`aborted_by_brain`, `escalation_limit`, `repeated_step`, or `max_steps`. Telemetry is recorded
per role, so the end-of-run table shows **brain vs hands** cost/tokens/time
separately — the seed of the later model bake-off. Run the single-model loop
instead with `relay run --solo hands`, or preview a plan before any writes with
`--confirm-plan`.

## Status — v0.0.31 (fix the read→edit/write loop: Relay's tools were sabotaging capable models)

A real task ("add `generateTerrain()` to `lunar-lander.html`") **looped and failed at 5/17 steps**:
the hands read the file, tried to edit/write/apply_patch, made no real progress, re-read, and looped
until the per-step budget (~12 model calls) was exhausted — then the brain replanned the same step and
Relay stopped (`repeated_step`). The **same models work fine in a bare harness**. A read-only
investigation (with an empirical CRLF repro) proved Relay's own **tools** were at fault — and **ruled
out** CRLF/hash and path-canonicalization: the read-before-edit guard hashes raw bytes on both sides and
canonicalizes paths consistently, so it is correct and CRLF-robust and was **left untouched**. Three safe,
platform-agnostic fixes:

- **`apply_patch` no-op honesty.** It reported `applied patch` without checking the file changed, so a
  hunk that *located* but produced identical content (context-only, or a `-`/`+` pair whose lines are
  equal) round-tripped unchanged yet reported success — the hands believed the edit took, re-read, and
  retried forever. `apply_patch` now reports `no change` (success prefix withheld) for a wholly-no-op
  patch and flags no-op sections in a mixed patch; a real patch is byte-for-byte unaffected, and a
  non-matching hunk still fails loudly.
- **A whole-file replacement is now visible.** `write`/`edit` overwrite the WHOLE file, so a model
  emitting a fragment silently replaced a multi-KB file with a few bytes. Overwriting an existing file
  now appends `— replaced entire file (was N bytes, M lines)`, so a fragment-clobber is visible to the
  hands and the reviewer; a new-file write is unchanged.
- **Executor steering.** The hands is told plainly that `write`/`edit` replace the whole file (a fragment
  destroys it) and to use `apply_patch` to modify part of an existing file.

The read-before-edit guard (`read_is_current`/`record_read`/`invalidate`/`content_hash`/`canonical`) is
**byte-for-byte unchanged**; the risky auto-refuse-on-small-fraction variant and the fuzzy-matcher
first-match bug are held for the backlog. All covered by network-free headless tests. Re-running the
lunar-lander task to confirm the loop is gone and the step completes is the maintainer's to verify; it is
not simulated here.

Under that, **v0.0.30** (TUI: one live stream, cyberpunk) still stands.

The two-pane TUI (a Conversation pane over an Activity pane — cramped and busy) is replaced by
**one live scrolling stream**, modeled on OpenCode's single-stream layout and improved with
Relay's brain/hands/findings distinction. Everything interleaves in the order it happens:
conversation (**you** / **brain**), the **plan rendered inline and live** (steps update **in
place** — ◉ done / ◍ active with a spinner / ○ pending), **tool calls** as compact `▸` lines,
**findings** (the v0.0.29 `<finding>` channel) as green lines, and review **verdicts**.
**brain = magenta, hands = cyan, findings = green, you = bright magenta.** The status line shows
a breathing mode LED (WORKING / AWAITING YOU) · step N/M · cost · cwd · queue — and the v0.0.28
**queue** is now visible (a queued count surfaced inline as items are added / consumed).
Cyberpunk is the **palette + activity-only spinners / LED only** — no CRT, scanlines, glow, or
ambient motion (it's a tool; motion happens only where there is activity). The splash screen and
the de-robotified voice ("what's next?") stay.

This is a **display-only** overhaul: **no engine / orchestrator / bridge behavior changed** — it
only surfaces state Relay already emits (including the v0.0.28 queue), and the brain↔hands split
is still recorded for tests + the `/log` bundle. The live feel (watching the stream flow, the
plan update in place, the spinner on active work) is the maintainer's to verify; the headless
tests pin the deterministic render path.

Under that, **v0.0.29** (three-pool memory: brain / hands / shared — Stage 1) still stands.

Relay's within-run memory was a **single pool, budgeted to the brain's context window, that
only the brain read** — the hands saw no memory at all. v0.0.29 splits it into **three pools**,
each budgeted to its **own** actor's window:

- **brain** — the brain's planning/reasoning memory; the hands never sees it; budgeted to the
  brain window.
- **hands** — the hands' private scratch; the brain never sees it. Written now; **read** by the
  hands only in Stage 2 (deferred).
- **shared** — the bidirectional, curated coordination channel both actors see: brain→hands
  **directives** (authoritative decisions/confirmations the hands must honor) and hands→brain
  **findings**. Budgeted to `min(brain, hands)` so it fits the smaller window.

A `MemoryBus` composes three **unmodified** `PlanMemory` stores, so per-pool dedup, counters,
and the snapshot shape carry over. Each actor's window is resolved independently
(`RELAY_HANDS_CONTEXT` for the hands; provider + catalog are passed so a known model gets its
real window, not the 8192 default). The brain reads **brain ∪ shared** with a **mechanized
union cap** — the rendered union is held within the brain-window budget, not merely asserted.

This is **Stage 1**: the user confirmation and the brain's self-answers **graduate to shared**
(so the hands finally sees authoritative decisions), and a new non-blocking **`<finding>`**
text tag lets the hands surface a bug / security issue / wrong assumption to shared without
ending its step. The hands reads **shared only** — never the brain pool, and not yet its own
pool (Stage 2 deferred) — so the narrow-context principle holds while the curated directives
reach it. The working directory stays separate session state (untouched). Text-protocol
throughout (`<finding>` is a tag; no native tool-calling); all covered by network-free headless
tests. The live feel — the hands acting on a shared directive, a `<finding>` reaching the
brain — is the maintainer's to verify; it is not simulated here.

Under that, **v0.0.28** (esc = interrupt with steer + queue; the session lives on) still stands.

**esc** is no longer "stop the run" — it is a full **interrupt**, and the **session is
never torn down** by it. esc halts the *run* at the clean executor-call boundary (the
v0.0.20 cancel-seam safety is intact: no mid-call teardown, clean worker join, money-leak
guard), then lands you at an interrupt prompt with the conversation, working dir, memory, and
cost tally all preserved. From there the fork is:

- **Type input → steer** (the default, since an interrupt usually means "fix this now"). A
  steer goes through the **brain**: the existing `replan` is invoked with your input as new
  direction, producing a revised plan for the **remaining** work (completed steps stay done),
  and execution resumes on it. A steer counts as a plan revision (bounded by
  `max_plan_revisions`). `/redirect <input>` is the explicit form.
- **Empty / esc again → stop**: the current plan is abandoned but the **session persists** —
  your next input begins fresh planning *within the same session*, never a teardown.

Alongside steer (apply now) is a fully functional **input queue** (apply next): **`/queue
<input>`** holds your input without interrupting the current step; when it finishes, the
queued item is consumed next (FIFO) as a new direction in the same session. **up-arrow** is
one unified **recall-and-edit** affordance across goals, steers, and queued items — recall a
recent input into the prompt, edit it, re-submit (or re-queue). The queue **mechanism is
complete**; its interim UI is deliberately minimal (a `queued: N` status segment) — surfacing
and polishing it is the upcoming UI-overhaul milestone.

**`/clear`** remains the distinct full-session **reset** (conversation, memory, plan, queue,
history wiped) — clearly different from **stop**, which keeps all of that and only abandons the
current plan.

Everything is text-protocol (steer reuses `replan`; no native tool-calling). All of it is
covered by network-free headless tests. The live feel — interrupting mid-run, steering with a
correction, queuing a next task, up-arrow editing a queued item, stop-vs-`/clear` — is the
maintainer's to verify; it is not simulated here.

Under that, **v0.0.27** (tool-hardening: apply_patch fuzzy matching, Windows/Unix commands,
housekeeping) still stands.

A live run on a multi-file Python project surfaced three real problems; that release fixed
each.

- **`apply_patch` now matches fuzzily** (it failed on its *first* real use with "a hunk's
  context did not match the file"). The matcher was exact-only, so the hands' near-misses —
  smart quotes, em-dashes, non-breaking spaces, trailing/leading whitespace — never matched
  and it fell back to a whole-file `write`. Hunk and `@@`-anchor location now use OpenCode's
  **four-pass progressive-leniency cascade** (exact → rstrip → trim → unicode-normalized,
  where the last pass maps smart quotes / the dash family / ellipsis / non-breaking space to
  ASCII), plus a trailing-empty-line retry. Each pass tries the **whole** pattern (never a
  substring), so it can't match a wrong location — a genuine mismatch still fails, and
  apply_patch stays atomic (validate-whole-then-apply) and per-section read-guarded.
- **Windows/Unix commands.** The hands runs on Windows but emitted `mkdir -p` (which Windows
  reads as a folder named `-p`). Three coordinated fixes: a shell-free, idempotent
  **`mkdir`** tool (`<mkdir path="..."/>` → `os.makedirs(..., exist_ok=True)`, cross-platform,
  creates directories only — and refused by the read-only brain); the **platform injected into
  the executor prompt** (on Windows it says there is no `mkdir -p`, use the tool) so the hands
  stops emitting Unix-isms and prefers tools over shell; and a prompt note that **`write`
  auto-creates parent directories**, so the hands stops `mkdir`-ing before writing.
- **Housekeeping.** A committed `scripts/test.sh` runs the suite and exits with *pytest's*
  status even when its output is piped, so a red suite can no longer be masked as green by the
  `pytest | tail` pattern (documented in Develop). And the flaky
  `test_clear_is_disabled_and_inert_mid_run` — which raced the periodic transcript-sync timer
  against a bare fake runner — now gives the fake an empty transcript and passes reliably.

All three are covered by network-free headless tests. The live feel — re-running the notes-CLI
project and watching `apply_patch` actually apply with no `mkdir -p` failures — is the
maintainer's to verify; it is not simulated here.

Under that, **v0.0.26** (Tier-1 tool expansion: `write`, `glob`, `apply_patch`, `webfetch`)
still stands.

The hands had to funnel almost everything through `bash` — fragile, cp1252-hazardous on
Windows, and hard to parse. This release gives the executor four more first-class tools, all
**Relay text-protocol tags** parsed by `parse()` and executed via `Tools` (never native
tool/function-calling — the moat), each advertised in the executor prompt so the hands knows
when to reach for it.

- **`write`** — `<write path="...">FULL CONTENTS</write>`: create or replace a whole file in
  one action. It threads the v0.0.23 read-before-edit guard: creating a **new** file needs no
  read; **overwriting an existing** file requires a current read first (don't blind-clobber a
  file you haven't seen), and a successful write invalidates the recorded read.
- **`glob`** — `<glob pattern="**/*.py"/>` (optional base dir): fast stdlib `pathlib` path
  matching so the hands stops shelling out to `find`/`ls`. Returns paths relative to the
  working dir, one per line, bounded (200, with a truncation note); read-only.
- **`apply_patch`** — `<apply_patch>*** Begin Patch … *** End Patch</apply_patch>`: OpenCode's
  **exact** envelope (`*** Add File` / `*** Update File` [+ `*** Move to:`] / `*** Delete File`,
  with `@@` anchors and `-`/`+` lines), for targeted multi-location / multi-file / rename /
  delete changes. It is **additive — `edit` is retained** — and **atomic**: the whole patch is
  validated first (sections parse, Add targets free, Update/Delete targets exist, every `@@`
  hunk locates) and applied all-or-nothing; no half-applied patches. The read guard is honored
  **per section**: Add is exempt, every Update needs a current read (else the *whole* patch is
  refused), Delete needs existence; on success every written/moved/deleted file's read is
  invalidated.
- **`webfetch`** — `<webfetch url="https://..."/>`: fetch a URL as readable text (stdlib
  `urllib` + a minimal HTML→text pass; no new dependency). The one network-touching tool:
  read-only GET, a real User-Agent (the default urllib UA is 403'd by some hosts), a 15s
  timeout so it can't hang the loop, output bounded to 4000 chars with a truncation note, and
  **friendly-on-error** (a concise `webfetch failed: …`, never a raw traceback). The network
  call sits behind a seam so the tests are network-free.

All four are covered by network-free headless tests; the read-only-brain investigation loop
explicitly refuses `write`/`apply_patch` (it can never write). The live feel — the hands
actually using `glob`/`apply_patch`/`webfetch` in a real run — is the maintainer's to verify;
it is not simulated here.

Under that, **v0.0.25** (trust/feel fixes: conversational plan reaction, session-sticky
working dir, full multi-line paste) still stands.

Three things made Relay feel robotic or untrustworthy in real use; that release fixed all
three.

- **The brain interprets your plan reply — no keyword match.** When the brain proposed a
  plan and asked "does this look good?", the reply was run through a hardcoded affirmative
  **keyword match**: only `ok`-style words committed, so a natural "yes that looks perfect!"
  did *nothing*. That's backwards — there's a language model right there that understands
  intent. The commit gate is now the brain's **interpretation** of your reply
  (`_classify_reaction`): it emits a `<reaction>` of `approve` | `approve_changes` | `revise`
  | `reject` | `unclear` (reusing the `call_model("brain", …)` path and the text-tag protocol,
  never native tool-calling). **approve** commits; **approve_changes** folds the tweak you
  asked for in the same breath into the plan *then* proceeds (never committing the un-modified
  plan and losing your change); **revise** re-plans and re-presents; **reject** aborts cleanly;
  and an **unclear** reply makes the brain ask one brief clarifying question rather than guess
  — committing spends money and edits files, so an ambiguous reply is never assumed to be
  approve (an unparseable classification also falls back to *unclear*, never *approve*). The
  round cap is unchanged, so `--confirm-plan`'s single-round behavior still holds. No
  affirmative-literal commit gate remains.
- **The working directory is session-sticky.** You'd create a folder, say "work inside it,"
  the brain would draw up a plan, you'd cancel it and start a new goal — and Relay forgot the
  working directory, building in the launch root instead. The cwd was per-run and **always
  re-defaulted to the immutable launch root each goal**: `Tools` pins every `bash` call to
  `cwd=project_root` (so a `cd` never persists), and the TUI built every run from `self._root`,
  which was never updated — so "work inside lunar_lander_testing" could only live as steps in a
  plan, and cancelling that plan evaporated the intent. Now a `WorkingDirSession` holds the
  effective working dir as **session state**: once established (set explicitly via the new
  `/cwd` command, or adopted from a **completed** run) it persists across subsequent goals
  until changed, and each run's root is threaded from it. The guard: adoption is **gated on a
  completed run**, so a cwd change that lived only in a never-executed (cancelled) plan can
  never silently take effect. The current working dir is surfaced in the status line
  (`cwd=…`).
- **A multi-line paste is captured in full.** Pasting multi-line text into the prompt kept
  only the **first line** — Textual's single-line `Input` truncates a paste to
  `splitlines()[0]`, silently dropping the rest and corrupting a pasted goal/spec. `PromptInput`
  now captures the **whole** paste (a newline within a paste is content, not a submit, so it
  never submits on its own), while typed **Enter-to-submit is unchanged**.

All three are covered by network-free headless tests (the brain reading each reaction
category; the session-sticky cwd persisting across goals while a cancelled-plan change does
not; a multi-line paste landing in full while typed Enter still submits). The live feel — a
natural "looks perfect!" committing, pasting a multi-line goal, working in a subfolder across
goals — is the maintainer's to verify in a real session; it is not simulated here.

Under that, **v0.0.24** (the agentic reviewer: a read-only brain-investigation primitive,
wired to the reviewer first) still stands.

The brain's step reviewer was the worst of a cluster of one-shot "judge-from-a-snippet"
calls: it **gated every step** yet literally **could not see the file it judged** — it got
a path label, a byte-count, and the hands' `<done>` claim, nothing more. The documented
failure: the hands writes a *correct* file, the blind reviewer rejects it, the plan
re-issues the step, and the loop grinds. This release fixes that by making the reviewer an
**agent that reads the real work before verdicting** — and does so by first building a
**generalized, reusable read-only brain-investigation primitive**, then wiring the reviewer
through it as its first consumer.

- **A generalized read-only brain-investigation loop (`relay.investigation.investigate`).**
  The brain analog of the executor's mini-loop, but **read-only** and consumer-parameterized.
  A consumer supplies a system prompt, a seed, terminator tag(s) (e.g. `<verdict>`), a
  final-output parser, a small budget, and a safe default; the primitive owns the loop, the
  **read-only action set** (`read`/`list`/`grep` only — an `edit`/`bash` a model emits is
  **refused and never executed**, so the brain can never write), budget bounding, bounded
  parse-failure nudging, and the **text-protocol contract** (Relay tags it parses itself —
  never native tool/function-calling, the moat). Built **heavy on purpose** and
  **validated on paper against all four** anomaly-cluster consumers (reviewer, `replan`,
  `evolve_plan`, `answer_or_escalate`) via documented adapter sketches, so "general" is
  earned, not hoped — but **only the reviewer is wired now**; the other three are
  proven-to-fit future consumers, unchanged functionally.
- **The reviewer, wired through it.** Seeded with the file(s) the executor actually changed
  (`touched_paths`, collected from the step's successful edits), the reviewer's strong first
  move is "read what changed" — the ground truth it lacked. It is bounded by a new
  `max_review_steps` budget (default 4 — it verifies, it doesn't build); on exhaustion it
  returns the existing **safe `accept`** default so running out of investigation budget never
  blocks progress; it returns the unchanged `StepReview` type (the orchestrator's branching
  is untouched); and it **never writes**. This is **phase (a)** — the touched file(s), the
  minimum that fixes the loop; **phase (b)** (widening to related files / reusing command
  output already in the transcript) is designed-for, a later prompt/seed/budget change, not
  a re-architecture.
- **The call-count invariant, restated (not silently changed).** Review is now **1..N brain
  calls** — 1 when the model verdicts immediately, up to `max_review_steps` when it
  investigates first — and review calls **remain excluded** from the executor
  `max_total_steps` ceiling (the reviewer has its own budget). The augmented review prompt
  keeps the `supervising an EXECUTOR` phrase verbatim, so the existing fakes still route
  review calls; every existing call-count test survives unchanged.

The brain investigation loop is **read-only and cannot write** (enforced structurally:
`edit`/`bash` never reach execution); review calls stay **off** the executor ceiling; **only
the reviewer is wired** (the other three consumers are paper-validated); and the **text
protocol**, not native tool-calling, is used throughout. Proven by network-free headless
tests — the primitive in isolation (reads, bounds, safe-default, read-only refusal, bounded
parse-failures), the reviewer reading the touched file before verdicting, the loop-fix
scenario (a correct file a blind reviewer would reject is accepted once read), and
budget-exhaustion → safe accept. The **behavioral** check — a before/after accept-rate
run-matrix on real projects, to confirm the agentic reviewer doesn't become net-stricter on
healthy runs — is a **live** task for the maintainer; it is not simulated here.

Under that, **v0.0.23** (read-before-edit: the hands can't blind-edit a file it hasn't read)
still stands.

A cheap/weak executor will happily rewrite a file from an assumed picture of its
contents — and if the brain's instruction is wrong about what the file looks like, that
mistake gets baked in silently. This release adds the Claude-Code-style **"must read
first" discipline**, scoped to Relay's hands: the executor may not `<edit>` an **existing**
file it has not **read** (with a still-current read) earlier in this run.

- **A hard guard where edits execute.** When the hands issues an `<edit>`: a **new file**
  (path doesn't exist) is allowed unconditionally — creation is never blocked; an **existing
  file with no current read** is **refused** with a recoverable observation (*"Refused: you
  must read "…" before editing it … Use `<read path="…"/>` first."*) and the file is left
  untouched; an existing file **with a current read** is applied. The refusal is a normal
  executor observation — **not** a parse failure or step abort — so the loop continues and
  the hands recovers by reading, then editing.
- **Freshness is content-hash based, never mtime.** A read records the file's sha256; a read
  stays valid only while the file's contents are unchanged (so unchanged files need no
  pointless re-read), and **any edit invalidates** the file's recorded read so the next edit
  needs a fresh one. Content hashing is robust where mtime isn't (e.g. Windows). The
  read-map is **run-scoped** (a read in an earlier step stays valid across steps).
- **One sentence to the executor prompt (catch the brain).** *"Before editing an existing
  file, read it first; if its actual contents don't match what the step assumes, say so
  rather than forcing the change."* The guard forces the contents into view; this nudge
  turns that into the hands reliably **speaking up** when the brain is wrong, instead of
  silently complying.

The guard is the **hands' only** — the brain is read-only (it plans/reviews, never edits)
and is untouched, as are the bridge, cost/limits, providers, and dependencies. Proven by
network-free headless tests (helper freshness lifecycle + executor behavior driven through
`run_planned`). One existing fixture that re-edited a file without reading was updated to
read-then-edit, matching real behavior.

Under that, **v0.0.22** (`/log`: a shareable, redacted debug export) still stands.

When a beta tester hits a problem, "describe what went wrong" loses most of the picture.
**`/log`** captures it instead: one timestamped, human-readable Markdown file with the
whole run — config, outcome, the conversation, the activity feed, the plan, and memory —
that they can attach to a GitHub issue. Its non-negotiable property is that it is **safe to
paste in public**: no API key (or other credential) can appear in it, **by construction**.

- **A redactor built and tested first (the safety core).** `redact_secrets(text,
  known_secrets=…)` is a pure, **idempotent**, crash-safe function that masks credential
  shapes — `sk-…` keys (OpenRouter/DeepSeek), `Bearer` tokens, and values following
  `api_key=` / `Authorization:` markers — and, the strongest guarantee, replaces any
  **exact live key value** handed to it **verbatim wherever it appears**, even off-pattern
  and mid-sentence. It's `None`-safe, leaves ordinary text untouched (bare git SHAs and
  hashes survive), and runs over **every field** of the bundle — belt-and-suspenders.
- **A bundle assembled from existing state — zero model calls.** The builder reads what the
  app already holds (no generation, no network, no upload) into clear `##` sections:
  environment (Relay/Python/OS), resolved **non-secret** config (models/providers/assumption/
  step ceiling + key **presence only**, never a value), the run outcome (status, cost,
  done/failed/total, which limit fired), the conversation transcript, the activity log, and
  the final plan + memory. The whole thing is then run through the redactor before writing.
- **Two scopes, picked from a dialog.** **Current project** exports the most recent run;
  **Full session** exports the session-spanning buffers plus the current project's structured
  outcome (Relay keeps no per-project history — the header says so). The choice writes
  `relay-debug-<YYYYMMDD-HHMMSS>.md` to the working directory and names the **full path**:
  *"…safe to attach to a GitHub issue (no keys included)."* A write failure reads friendly,
  not as a traceback. **Relay never sends the bundle anywhere** — the file is local and you
  choose whether to share it.

The export path makes **no model call** and changes no engine/bridge behavior — it's
read-only surfacing of existing state plus one new command. Proven by network-free headless
tests, including a bundle that seeds a fake key into **every** field and comes out with the
key absent from all of them.

Under that, **v0.0.21** (fail fast on a stuck loop: memory dedup, repetition breaker,
friendly stop, step ceiling) still stands.

A repeated-dead-end loop showed up in live use: on a step the brain wouldn't accept,
it kept re-deriving the same memory and the plan kept re-issuing the same step until
the escalation budget ran out — tens to ~170 model calls on one step that never
advanced. This release makes the agent **fail fast and cleanly** on that loop and stop
reinforcing it. (The deeper *why it rejects at all* — the reviewer judging a truncated
action transcript — is a separate, deferred fix; the reviewer's context is untouched here.)

- **Memory near-duplicate suppression.** Plan memory no longer stores a fact/decision
  that merely **restates** a recent same-kind entry, so the brain's rewordings ("main()
  lacks the input loop" ×3, a repeated verbatim decision) can't accrete and dominate the
  keyword-ranked slice fed back into the next review. Conservative — same-kind, a bounded
  recent window, content-token overlap with a **novelty guard** so a genuine superset that
  *adds* information is always kept.
- **Repetition breaker.** When a replan re-issues a step **near-identical to one that
  already dead-ended**, that *is* the loop — Relay stops on a new `repeated_step` terminal
  **before** re-running it, instead of grinding the full escalation budget. It fires only
  on a clear repeat, so a progressing run whose replan picks a genuinely different approach
  is never short-circuited.
- **Friendly, plain-language stop.** The stuck-loop and step-ceiling terminals now read in
  plain terms — *what happened* and *2–3 things to try* (a more capable model via
  `/model` / `/provider`, smaller/clearer steps, or review the partial work) — instead of
  `escalation_limit` jargon. One shared source feeds every surface (TUI + CLI); the internal
  status strings are unchanged for logs/tests.
- **One comprehensible step ceiling.** The global executor-step cap is on by **default 50**
  in production (CLI + TUI) — a generous safety net that should never fire in normal use,
  not a wall. Raise or disable it via `--max-total-steps`, the `RELAY_MAX_TOTAL_STEPS` env
  var, or `config.json` (`0`/`off` = unbounded; precedence env > config > default), and the
  ceiling message tells you how.

Under that, **v0.0.20** (live cost counter, `/cost`, fast esc-to-stop) still stands.
Relay's philosophy on spend is explicit: it does **not** impose a cap or judge what
you want to build — it shows cost live and lets **you** stop whenever you decide. That
release surfaces two cost figures and makes stopping feel instant.

- **A live per-goal cost counter** in the status line (e.g. `$0.0234`), animated with
  a brief highlight as it climbs and toggleable via `/cost`. It resets when a new goal
  starts but keeps showing the **last goal's total while idle** (it never blinks to
  `$0` on finish), and shows an **`esc to stop`** affordance while a run is in flight.
- **A session-cumulative total** that accumulates across all goals, surfaced and
  **resettable** via the new **`/cost`** command (which also toggles the live counter).
  A new goal doesn't clear it; it's cleared only on quit or a deliberate reset.
- **Fast `esc`-to-stop.** Pressing `esc` acknowledges **instantly** (`stopping…` in the
  status line + activity — never silent) and halts at the next **executor-call**
  boundary inside a step, not only at step boundaries — so a long multi-call step stops
  within ~one call's latency. The in-flight call is **never torn down** (its tokens are
  already committed); we stop cleanly before the next one. The money-leak guard and the
  clean worker-join are untouched — this is a finer cancel-check point, nothing more.

All cost data already existed (`Ledger.total_cost()`); this is pure surfacing plus a
finer cancellation boundary — **no spend cap, no new model calls, no judgment.** The
per-run finished-line cost is unchanged; the two counters are additional.

Under that, **v0.0.19** (surface fixes: reachable slash, friendly errors, honest saves)
still stands — a pass over rough edges found in live use, at the boundary of real env
vars, live validation, display refresh, and interactive state. Two were genuine bugs;
three turned out already sound and are now pinned by regression tests:

- **`/` is reachable whenever the engine isn't generating.** The popover used to
  open **only** when idle, so once a goal was in flight you often couldn't reach
  slash commands. A single predicate (`_slash_allowed`) now opens it in idle **and**
  every awaiting-you state (react / decide / approve), suppressing it **only** during
  active planning/execution. The gate governs the popover alone — routing, the
  engine, the bridge, and the `InputRouter` are untouched.
- **No raw provider JSON ever reaches you.** A provider 400 used to surface as a raw
  `Error code: 400 - {'error': {... 'raw': ...}}` blob. Every point an error can reach
  the UI — the run-error line, the slash validation note, the setup rejection, the
  `/doctor` preflight — now renders a friendly one-liner (what failed, which
  provider/model, a hint to re-pick) via `friendly_provider_error`; the raw payload is
  dropped. Clean notes pass through unchanged.
- **A shadowed save says so.** Saving a model via `/model` or `/provider` writes
  `config.json`, but `env > config` means a `RELAY_*_MODEL` env var (or a project
  `.env`) silently wins — so the screen looked unchanged. The live display already
  reloaded correctly; now `config.env_override_for` lets the app add an honest note
  naming the overriding variable instead of looking stale. Precedence is **unchanged**
  — this only reports the shadow.

Pinned by new tests (no behavior change — already correct on v0.0.18): key
**presence** mirrors `resolve_key`, so a key counts as present when it resolves from
the env var **or** `auth.json` (an env-key user is never wrongly sent to first-run
setup); and `/provider` validates + persists against the **newly-picked** provider in
both directions, never the role's stale one.

Under that, **v0.0.18** (`/provider`, a reusable segmented toggle, richer `/assume`)
still stands — two additions on the v0.0.17 slash infrastructure, plus one new
reusable primitive:

- **`SegmentedControl`** — a general horizontal choose-one toggle (the analog of
  `SelectDialog` for a small fixed set): **left/right** cycle with wrap-around,
  **Enter** commits, **Esc** cancels. Built as a proper component (its own tests),
  reusable by any future step — `/provider`'s role step is just its first consumer.
- **`/provider`** — set which provider supplies a role, then its model. A role
  toggle (**`brain` ◄ ► `hands` ◄ ► `both`**) → the provider `SelectDialog` (the
  same list `/key`/setup use) → straight into the **shared model-pick step** for
  the just-chosen provider (a live `/models` list for DeepSeek, a validated slug
  for OpenRouter). **Per-role isolation**: the chosen role is the only one touched.
  **`both` runs the model pick twice** — brain, then hands — each self-contained,
  so you can pair (say) a pro brain and a flash hands on the same provider. Provider
  + model both persist to `config.json` via the shared `persist_role`.
- **`/assume`** now shows a short, plain-language **description per level**, derived
  from the real dial semantics in `config.py` (`assumption_summary`, sourced from
  the actual directive text so it can't drift) — e.g. "1 — super loose: assume
  almost everything", "5 — exact letter: assume almost nothing". The current level
  is still marked.

It's **reuse, not rebuild**: `/provider`'s provider dialog, model-pick step,
`validate_model`, `list_models`, and `persist_role` are the SAME pieces `/model`
and the setup screen use (`/model` was refactored to share the model-pick step).
No inline arguments anywhere; the engine/InputRouter/precedence are untouched.

Under that, the v0.0.17 **slash control plane** still stands — type **`/`** in the
prompt for a filterable command popover; every command opens a dialog or runs a
clean action, none parse inline args, and a key is never typed into the chat input:

- `/help` — list every command (the discoverability anchor).
- `/model` — pick a role, then its model: a **live `/models` list** for DeepSeek,
  a **validated slug field** for OpenRouter; persisted + live-reloaded.
- `/provider` — set a role's provider, then chain into its model pick (above).
- `/key` — a **masked** key-entry dialog (`password=True`), saved `0o600`.
- `/config` — the resolved config (provider/model/thinking + source; key
  present/absent — **never the key**).
- `/doctor` — the provider/model preflight in a dialog.
- `/runs` — recent runs, read-only.
- `/assume` — pick the assumption level (1–5 / auto), each with a description.
- `/cost` — session + per-goal spend; toggle / reset the live counter.
- `/log` — export a **redacted** debug `.md` (current project / full session) to
  share when reporting an issue; key **presence only**, never a value.
- `/clear` — clear the panes (disabled mid-run; never clobbers a live run).

Under that, **beta-enablement** (v0.0.16): a user who has never touched a
`.env` can add a provider, enter a key, and pick models *in the app*. Config now
persists to two deliberately separate files in your OS user-config dir
(`%LOCALAPPDATA%\relay` / `~/.config/relay`, via `platformdirs`):

- **`config.json`** — inspectable, non-secret selections (provider/model/thinking
  per role) plus reserved picker sockets (`cost_bias`, `recommendations_source` —
  round-tripped but inert; the recommendation engine is the next milestone).
- **`auth.json`** — credentials, written `0o600`, keyed by provider as a record
  (`{"type":"api","key":...}`, room for OAuth later). All secret handling is
  isolated in `relay/secrets.py`; a key is **never** written to `config.json`,
  logged, printed, or put in a run record.

**Precedence preserves the env/.env workflow as highest** — models resolve
`env > config.json > default`, keys resolve `env-key > auth.json` — so a developer
with `RELAY_*` / `OPENROUTER_API_KEY` set is unaffected; absent/corrupt files fall
through harmlessly. Provider profiles gain a **`discovery`** mode: OpenRouter is
`manual` (type any slug, validated live), DeepSeek is `list` (enumerates live via
`/models`, so deprecations self-correct).

Manage it from the CLI — `relay config show` (resolved values + source per role +
key present/absent, **never the key**), `set-role` (validates the slug live before
saving), `set-key` (entered **without echo**, stored `0o600`), `remove-key`,
`list-models` — or in the TUI **setup screen** (`ctrl+s`): masked key entry,
per-role model pick (a live list for DeepSeek, a validated slug for OpenRouter), a
thinking toggle. An empty **first run** is guided into setup (offered-but-prominent;
a configured/env user goes straight to chat).

Under that, the **TUI polish** pass (v0.0.15) still stands: a rotating state-aware
placeholder, the proposal split (conversation = headline + assumptions; activity =
the numbered plan), and the attributed brain↔hands activity feed (zero new tokens).

And under that, Relay is genuinely **multi-provider** (v0.0.13). Three backend
pieces:

1. **A model catalog** (`relay/catalog.py`). Model metadata + pricing are pulled
   from an external catalog (default [`models.dev`](https://models.dev), endpoint
   `/api.json`), validated, cached to disk, and served via a small lookup API
   (cost / capabilities / context-limit / list-models). A **fallback chain** means
   a network blip can never brick Relay or zero out cost: fresh disk cache →
   network fetch → stale cache → a small **bundled fallback** (DeepSeek + OpenRouter
   pricing baked in). The rung actually used is surfaced as the catalog *status*.
2. **Provider profiles** (`relay/providers.py`). A provider is a thin
   `{id, base_url, key_env}` entry over the one shared OpenAI-compatible client —
   adding one is a registry line, not new code. The provider is chosen **per role**
   (`RELAY_BRAIN_PROVIDER` / `RELAY_HANDS_PROVIDER`), defaulting to `openrouter` so
   **every prior behavior is byte-for-byte unchanged**.
3. **DeepSeek direct** — the first non-OpenRouter provider, with **catalog-driven
   cost** that respects DeepSeek's cache hit/miss split (`prompt_cache_hit_tokens`
   priced at the cache-read rate, `prompt_cache_miss_tokens` at the input rate). A
   naive single-rate calc is wrong by up to ~50× once Relay's reused prompt prefixes
   start hitting DeepSeek's cache, so the split matters. Thinking mode is **off by
   default** and per-role-toggleable.

`relay doctor` is now provider-aware: it preflights each role against *its own*
provider's API, prints the catalog source/status, and reports the resolved context
window + source per role. The text protocol stays the universal execution mechanism
(no native tool-calling), and the OpenRouter path is untouched.

Under that, v0.0.12's TUI visual-polish pass still stands:

`relay tui` opens on a composed, cyberpunk-terminal **welcome screen** — the
letterspaced `RELAY` block wordmark hero, a rotating greeting, the brain/hands
pairing promoted as identity, and a dim keybind hint — that **glitch/datamosh-
transitions** into the two working panes when you send your first goal. A short
boot decode resolves into the wordmark on launch; the handoff to the panes is a
~400ms dissolve. It's a **look-only** pass: no engine/bridge change, and the run
kicks off immediately (never gated on an animation). Onboarding, the model
picker, and the experience dial are still ahead.

Under that, `relay tui` is the **two-pane Textual chat** over the engine: the
conversation thread on top, the live execution feed below, one input box routed
by what the engine is waiting for. The hard part (v0.0.11) is the **sync ↔ async
bridge** (`relay/bridge.py`): the blocking engine runs on a worker thread and
never learns it's talking to a TUI; the UI never blocks. A coarse **cancel**
(step-boundary `cancel_check` → status `cancelled`) and a clean quit (cancel +
join, never an orphaned worker still billing the API) are the money-leak guards.
The plain CLI is fully intact — the TUI is additive.

Underneath: planning is a **conversation** (v0.08 A) and a user-owned
**assumption dial** biases *every* assume-vs-ask decision — both the
conversation and the autonomous loop. v0.08 B fuses the two "the brain is
asking me something" modes into **one continuous transcript**: the planning
dialogue and the mid-run escalations are turns in the same thread, so a product
decision raised mid-run reads as a continuation of the conversation, not a
context-less popup. Plan memory **derives from** the transcript, and the
transcript compacts toward *readability* (recent verbatim, older folded into a
readable narrative).

**What exists now:**

- `relay/client.py` — the one place that touches the OpenAI-compatible SDK; **provider-aware** `build_client(provider)` (defaults to OpenRouter).
- `relay/providers.py` — **provider profiles**: `ProviderProfile {id, base_url, key_env}` + registry (`openrouter`, `deepseek`), `resolve_provider`.
- `relay/catalog.py` — **the model catalog**: fetch → validate → cache → serve model metadata + pricing, with the fresh→network→stale→bundled fallback chain; cost / capability / context-limit lookups.
- `relay/config.py` — **per-role** provider + model + thinking mapping, **and the assumption dial** (`resolve_assumption_level`, `assumption_directive`).
- `relay/telemetry.py` — `CallRecord` / `Ledger` recording tokens, cost, latency, and parse-failure count, **split per role**.
- `relay/models.py` — `call_model(...)`, **the seam** everything else builds on; selects the role's provider and extracts cost per provider (OpenRouter's returned cost; DeepSeek's cache hit/miss split via the catalog).
- `relay/protocol.py` — the text action protocol + a tolerant `parse()` (`<plan>`/`<step>`, `<abort>`, `<blocked>`, `<question>`).
- `relay/policy.py` — the command policy: `classify()` → `BLOCKED` / `CONFIRM` / `ALLOW`.
- `relay/tools.py` — `read` / `list` / `grep` / `edit` / `bash`; `bash` consults the policy and an approver.
- `relay/loop.py` — `run_task(...)`, the single-model loop (kept for `--solo`).
- `relay/memory.py` — **plan memory**: `PlanMemory` of dual-fidelity `MemoryEntry` values, budget-bounded `relevant(...)`, compress-not-truncate `compacted_context(...)`.
- `relay/context.py` — **context-window awareness**: `resolve_context_window(...)` (override → catalog `limit.context` → OpenRouter metadata → local probe → default).
- `relay/planner.py` — **the brain**: `make_plan` / `replan` / `evolve_plan`, `review_step` (supervise), `answer_or_escalate` (answer-vs-escalate, dial-biased).
- `relay/conversation.py` — **conversational planning**: `plan_conversationally(...)` — scope assessment, posture, dual-fidelity proposal, free-form reactions, commit; appends its turns to the shared transcript.
- `relay/transcript.py` — **the continuous transcript**: `Transcript` of `Turn` values (the human-facing source of truth), `record_decision` (transcript-first, memory-derived), and readability-preserving compaction (`compact_transcript` / `render_for_brain`).
- `relay/orchestrator.py` — **the autonomous loop**: `run_planned(...)` (committed plan + the dial + the shared transcript; escalations continue the thread; step-boundary `cancel_check`).
- `relay/runlog.py` — **durable run records**: `RunRecord` + `build_record` / `append_record` / `load_records` (JSONL).
- `relay/bridge.py` — **the sync↔async bridge**: `EngineBridge` (blocking asks ↔ thread-safe handoff), `EngineRunner` (the conversational arc on a worker thread), `InputRouter` (the input state machine). UI-framework-free; tested headless.
- `relay/tui.py` — **the TUI**: a composed welcome screen (the `RELAY` wordmark hero, rotating greeting, promoted model identity) that glitch/datamosh-transitions into a two-pane chat (conversation + activity); one routed input box, the `present_prompt` chokepoint, cancel + clean shutdown.
- `relay/cli.py` — `relay models`, `relay demo`, `relay run` (conversational, `--assume`, `--show-transcript`), `relay tui`, `relay runs`, `relay doctor` (**provider-aware**: preflights each role against its provider, shows catalog status + per-role context window).
- Network-free tests across the whole stack (incl. conversation, the dial, the continuous transcript, the bridge, the headless TUI, **the catalog + fallback chain, provider selection, and the DeepSeek cost split** — all via a local catalog fixture, no sockets).

## Conversational planning + the assumption dial

`relay run` no longer plans in one shot — it plans *with you*:

1. **Scope assessment (visible, logged).** The brain's first move judges how much
   of the goal is consequential AND undetermined: `small` (self-contained),
   `large` (forking), or `ambiguous` (can't tell which). Reading scope off a goal
   string is error-prone ("website" spans three orders of magnitude), so it's an
   explicit step, not a silent classifier.
2. **Posture follows scope + the dial.** small → *propose-fast* (state a full plan,
   assumptions exposed); large → *elicit-first* (ask the few highest-stakes,
   genuinely-undetermined questions, then propose); ambiguous → ask *one* scoping
   question first. Questions are restrained on purpose: only what is consequential
   AND genuinely ambiguous AND something you can actually judge — the brain just
   decides the noise ("retry 3 or 5?") and records it.
3. **Free-form reaction loop.** You react in plain language ("make it dark mode,
   drop the login"); the brain folds it into the plan and re-renders, until you
   commit (or a round cap halts). On commit, the finalized plan hands to the
   autonomous loop. `--confirm-plan` is the degenerate 1-round case.

**Dual-fidelity is DERIVED, not parallel.** The plain plan you see and the precise
executor spec are one artifact — the plain version is *derived from the exact
steps*, never generated independently. If they were produced separately they would
drift, and you'd approve a friendly sentence while a different reality got built.
The consequential additions the brain made compiling vague intent ("make login
simple" → "Google sign-in only, no passwords") are surfaced as assumptions to
confirm.

### The assumption dial (`RELAY_ASSUMPTION_LEVEL` / `--assume`)

A single user-owned setting biasing how much the brain assumes vs. asks —
threaded into **both** the planning conversation and the autonomous loop's
`answer_or_escalate`, so you're asked consistently everywhere.

| Level | Behavior |
| --- | --- |
| `1` | Super loose: assume almost everything, act on intent, ask almost nothing. |
| `2`–`4` | Increasing caution between the extremes. |
| `5` | Exact letter: assume almost nothing, follow instructions literally — but still surface genuine impossibilities/contradictions (e.g. "add this Python library" in a JS project). |
| `auto` (default) | Normal mode: the brain decides per-question whether to assume or ask. |

`auto` is its own mode (the threshold handed back to the brain), **not** a numeric
midpoint. Honest note: level `1` deliberately weakens pre-execution oversight —
you've chosen "build it, I'll react to the result," shifting the safety net from
pre-confirmation onto correction-after-seeing. Relay honors that; it does not
"protect" a level-1 user by asking anyway.

## The continuous transcript (one thread, planning → execution)

Before this, you met the brain twice — once while planning, again (separately)
when a decision escalated mid-run. Two popups, no shared memory of the
conversation. Now there is **one `Transcript`**: an ordered, append-only thread of
`Turn`s (`speaker`, `phase`, `text`, a monotonic `created_at`, optional `refs`)
that spans the whole run. The planning proposal, your reactions, the commit, any
mid-run escalation **and** your decision, and a closing result turn are all turns
in the same thread — so a question raised mid-execution is just the next thing the
brain says, phrased *as a continuation* ("earlier you said you wanted this
simple, so…"). When composing an escalation the brain is handed a window-bounded
slice of the recent thread, so it stays conversationally coherent.

The transcript holds the **conversation**, not an execution log — granular tool
calls stay in the event stream / `runs.jsonl`, never as turns. A `proposal` turn
carries a plain one/two-sentence **headline** (e.g. *"A single-file Python todo
CLI with add/list/done/delete and JSON storage"*), not the full executor spec —
the headline is emitted in the same generation as the plan (no extra brain call)
and the full steps stay in the `Plan` (linked via the turn's `refs`), so
scroll-back reads as prose rather than walls of implementation detail. The closing
result turn is honest about partial outcomes: a run that recovered from a failed
step does not claim it "built everything."

**Transcript-first, memory-derived.** There are now two stores that both compact,
and they must not drift. The transcript is human-facing, chronological, and the
**source of truth for what was said and decided**; plan memory is the brain's
dense working extract. A user-facing decision is recorded to the transcript
**first** (via `record_decision`), then derived into a linked `MemoryEntry` whose
`provenance` points back at the turn (`transcript:<id>`). When the two disagree,
the transcript wins and memory is what gets re-derived.

**Compaction preserves readability.** The transcript must stay legible and bounded
over a long run, so it compacts — but *not* with plan memory's dense
`compacted_context` (that would wreck scroll-back). Instead, recent turns stay
**verbatim** and older ones fold into a **readable narrative** ("Earlier: you
asked for a todo app, the brain proposed a Flask backend, you changed auth to
Google-only, then committed."). Brain reads are window-bounded to the resolved
context window (`render_for_brain`); a post-execution pass compacts the thread to
its durable readable form (`compact_transcript`). A failing summarizer degrades
gracefully (keeps more verbatim, notes it) — it never crashes the run.

`relay run --show-transcript` prints the compacted thread after a run — the
plain-CLI preview of scroll-back. (`relay tui` is the scrollable interactive
view; the transcript is in-process for one run, but snapshot-shaped
—`to_state()` / `from_state()`— so cross-session scroll-back is cheap to add.)

## The TUI (part 1 of 2): the bridge + a minimal chat

**The TUI is just another renderer.** The engine already emits events
(`on_event`) and asks through blocking callbacks (`user_turn`, `user_decision`,
`approver`, `plan_gate`); the TUI renders the events and answers the callbacks.
No planning or execution logic lives in the UI.

The risky part is the **sync ↔ async bridge** (`relay/bridge.py`). Relay's
engine is synchronous and *blocks*; Textual's event loop must *never* block. So
the engine runs on a **worker thread** (`EngineRunner`: the full conversational
arc — `plan_conversationally` → commit → `run_planned` — on ONE shared
`Transcript`), and:

- **Engine → UI:** events and "I need an answer" requests are marshaled onto
  the UI thread (Textual's `call_from_thread`); nothing touches a widget
  cross-thread.
- **UI → engine:** each blocking callback parks the worker on a thread-safe
  handoff (`UiRequest`: a `threading.Event` + answer slot) until the UI
  delivers the user's typed answer — only the worker ever blocks. The engine
  never knows it's talking to a TUI instead of a terminal.

**One input box, routed explicitly.** `InputRouter` is a small state machine
(`idle` / `planning` / `executing` / `awaiting_reaction` / `awaiting_decision`
/ `awaiting_approval`): a submit in `idle` starts a goal; in an awaiting state
it answers the parked callback (a mid-run escalation is just the next turn in
the same box); while the engine is busy it's ignored, never misrouted.

**Layout (minimal, v1).** Conversation pane on top — the transcript thread
(proposal *headlines*, reactions, escalations, decisions, result), rendered
**unicode-clean** (no ASCII sanitizing on this path, unlike the legacy
console). Activity pane below — the noisy `on_event` firehose (steps, tool
calls, verdicts, memory writes), kept out of the conversation. Then a one-line
status + **model indicator** (the brain/hands pairing, visible from launch,
before the first message) and the input box. Every user-facing prompt passes
through one chokepoint (`present_prompt`) — prompt 2's experience-level
projection slots in there.

**Cancellation + clean shutdown (the money-leak guard).** `Esc` requests a
coarse cancel: a pending ask unblocks immediately; a running plan stops at the
next **step boundary** (`cancel_check` in `run_planned`, terminal status
`cancelled` — result turn + compaction still happen). Quitting cancels and
**joins** the worker (bounded wait) so no orphaned thread keeps calling the
API.

**The welcome state + glitch handoff (v0.0.12, look-only).** Launch lands on a
composed, cyberpunk-terminal welcome screen — a genuinely separate state, not
the working view with empty panes. The hero is the letterspaced `RELAY` block
wordmark; below it a rotating greeting (`GREETINGS`, one per launch), the
brain/hands pairing promoted as identity, and a dim keybind hint. A short boot
**glitch decode** resolves into the wordmark on launch; the first goal
**datamosh-dissolves** the welcome screen into the two working panes (~400ms,
short *always* — it fires every session). All animations route through one
mode-gated chokepoint (`"short"` live, `"off"` instant no-op, `"long"` stubbed
to short) so the next milestone drives the mode from persisted settings + a
launch counter without restructuring. The handoff is purely visual — the run
starts immediately, never gated on an animation, and no engine/bridge behavior
changes.

**Not here yet (part 2):** onboarding, the model picker (the indicator is the
read-only stand-in), the experience-level dial / question-rephrasing, the
config-persisted animation toggle + launch-counter (the `"long"` first-run
variant), diff viewer, theming, streaming tokens.

## Plan memory (within-run)

Plan memory is the brain's **within-run knowledge** — facts it discovered,
decisions it made (with rationale), user confirmations, and dead ends —
accumulated as a run progresses and discarded when the run ends. It is reasoning
state, **not** telemetry: it is in-process only and never touches `runs.jsonl`.
The autonomous loop (below) reads it window-aware when planning, supervising,
answering, and deciding to escalate, and writes to it as it learns.

Each `MemoryEntry` is **dual-fidelity** — a precise `detail` (for the brain and
executors) and a plain `summary` (for humans) — plus `kind`
(`fact`/`decision`/`confirmation`/`dead_end`), `provenance`, a monotonic
`created_at`, and optional `tags`. `PlanMemory` is an append log (entries are
never silently dropped), kept value-shaped so a point-in-time copy is cheap
(`to_state()` / `from_state()`).

The point is that memory is **queryable and sliceable, never a transcript blob**,
because it must shrink to fit any brain's context window:

- **Window-aware budget.** `resolve_context_window(model)` finds the brain's
  window; `memory_budget(window)` reserves headroom and allots a fraction
  (default 50%, minus a 1024-token reserve) to memory.
- **Budget-driven slicing.** `relevant(query, budget_tokens=...)` ranks entries
  (keyword/tag overlap + kind weight + recency — a dependency-light deterministic
  heuristic, no embeddings) and returns the most relevant that *fit the budget* —
  on a 200K window that may be everything; under an 8K-derived budget just a few.
  Same store, same code; the budget just tightens.
- **Compress, don't truncate.** When relevant memory overflows the budget,
  `compacted_context(...)` keeps the top entries verbatim and replaces the
  overflow with one brain-written compact summary (attributed in telemetry) — the
  *knowledge* survives even when the *detail* can't. If the summarizer fails it
  degrades to a noted hard-trim; it never crashes.
- **Honest small-window warning.** `small_window_warning(memory, window)` fires
  when the window is too small to slice the working set, so Relay says so rather
  than degrading silently.

### Knowing the window: declare → discover → default

Relay resolves the brain's context window in strict priority order, and discovery
never crashes a run (a failed step falls through):

1. **Declare** — `RELAY_BRAIN_CONTEXT` (env) or an explicit override always wins.
2. **Discover** — OpenRouter models report `context_length` via the API
   (cached per process); local runtimes (Ollama / LM Studio / llama.cpp) are
   probed best-effort.
3. **Default** — otherwise a conservative `8192`, with a visible note that Relay
   is guessing and you can declare it via `RELAY_BRAIN_CONTEXT`.

`relay doctor` prints the resolved window and its source
(`override` / `openrouter` / `local:<runtime>` / `default`).

## The autonomous loop

`relay run` drives an autonomous planner↔executor loop — the brain relays to the
executor itself instead of a human doing it:

- **Selective supervision (default on).** The brain reviews at **step boundaries**
  (and on anomalies), not on every executor action — one `review_step` call per
  step → `accept` / `follow_up` (a bounded corrective hand-back) / `revise_plan`.
  Supervision costs brain calls; it's a measurable tradeoff you can disable with
  `--no-supervise`.
- **Answer vs. escalate (the sharp edge).** When the executor emits a
  `<question>`, the brain makes an explicit, **logged** classification: *can I
  answer this from the code + memory (technical), or is it a genuine product
  decision for the user?* It answers itself when it legitimately can, and
  **leans to escalate when unsure** — a needless escalation is a mild annoyance,
  but a wrong self-answer silently builds the wrong thing. Escalations go to the
  `user_decision` seam (an interactive prompt in the CLI). With no seam available
  the run stops as `unresolved_escalation` rather than **guessing**.
- **Learns + evolves.** The brain writes what it learns to plan memory
  (step outcomes → `fact`, self-answers/reviews → `decision`, user resolutions →
  `confirmation`, dead ends → `dead_end`, each with provenance + dual form) and
  evolves the remaining plan (`evolve_plan`) when learning warrants it. All loops
  are bounded (`max_followups_per_step`, `max_plan_revisions`, `max_escalations`,
  and `max_total_steps` — the global step ceiling, **default 50**, raisable via
  `--max-total-steps` / `RELAY_MAX_TOTAL_STEPS` / config). A replan that re-issues
  an already-dead-ended step is caught and the run stops fast (`repeated_step`).
- **Everything is an event.** `executor_question`, `brain_self_answered`,
  `brain_escalated`, `user_decided`, `step_reviewed`, `plan_revised`,
  `memory_write` stream to the console so the brain↔executor exchange is visible.

> Product decisions are **never** auto-answered. `--auto-approve` only
> auto-approves `CONFIRM` bash commands; it does not answer escalated questions.

### The text protocol (never native tool-calling)

The model expresses actions as plain-text tags that Relay parses itself — it does
**not** use any provider's function/tool-calling API. This is deliberate: it
keeps *every* model (including ones with no function-calling support) in the
comparison set. Supported tags:

```text
<thinking>...</thinking>                 optional; captured, not executed
<read path="..."/>
<list path="..."/>
<grep pattern="..." path="..."/>
<edit path="...">...full new file content...</edit>
<bash>...command...</bash>
<question>...</question>                  executor: needs info to proceed (brain answers/escalates)
<done>...short summary...</done>         ends the (sub)step
<plan><step>...</step>...</plan>         brain: the ordered plan
<abort>reason</abort>                    brain: goal unreachable
<blocked>reason</blocked>                executor: stuck on this step
```

A message with no valid action and no `<done>` is a **parse failure** — recorded
in the ledger (parse-failure rate is a free model-quality signal) and nudged back
on track, aborting cleanly after a few consecutive failures.

## Command policy (the guardrail)

Before `bash` runs anything, `relay/policy.py` classifies the command into one of
three verdicts:

| Verdict | What happens | Examples |
| --- | --- | --- |
| **`BLOCKED`** | Refused outright, never run — **even with `--auto-approve`**. | `sudo …`, `rm -rf /`, `rm -rf ~`, fork bombs, `mkfs…`, `dd … of=/dev/sda`, `shutdown`/`reboot`, `curl … \| sh`, `chmod -R 777 /` |
| **`CONFIRM`** | Destructive but legitimate — paused for approval. | `rm -rf <in-project>`, `git push --force`, `git reset --hard`, `git clean -fd`, recursive `chmod`/`chown` in-project, `kill -9`, `pkill`/`killall` |
| **`ALLOW`** | Runs normally. | `ls`, `cat`, `grep`, `git status`, `npm test`, `python …` |

Compound commands are split on `&&`, `||`, `;`, `|` (and subshell parens) and each
segment is classified; the command takes the **most severe** segment's verdict, so
`ls && rm -rf /` is `BLOCKED`. Programs are matched by basename, so `/bin/rm` is
treated as `rm`.

`CONFIRM` commands are decided by an **approver**. In `relay run` the default is
interactive (a panel shows the command + reason and asks you to approve/deny);
`--auto-approve` / `-y` approves the `CONFIRM` category for unattended runs but
**never** affects `BLOCKED`. In non-interactive contexts with no approver, the
safe default is to **deny**. When a command is refused, the model sees
`BLOCKED by policy: …` or `DENIED …` as the observation and can route around it.

> ### Honest limits — this is a speed bump, not a sandbox
>
> The policy is **best-effort**. It classifies command *strings* with
> tokenization and patterns, which catches obvious accidents and the common
> destructive patterns a well-meaning model emits by mistake. It does **not**
> defend against an adversarial model actively trying to escape: environment-
> variable expansion, command substitution, `eval`, base64 payloads, here-docs,
> exotic aliases, and intermediate pipes can all evade pattern matching. Relay's
> `bash` is **not** sandboxed — path-confinement only pins the working directory,
> and string classification does not contain a determined command. The real
> boundary is process/container isolation, which is a **later milestone (v0.95)**,
> deliberately not this one. Treat this layer as what it is: it blocks obvious
> destructive commands and gates risky ones behind confirmation — nothing more.

**Intentionally NOT here yet** (later milestones): process/container **sandboxing**
of `bash` (v0.95); plan snapshot / fork / time-travel — the plan here is in-memory
and forward-only, so escalation replaces the remaining tail rather than branching
(v0.2); dual-channel human/machine rendering and experience levels (v0.15); the
run-matrix that sweeps model pairs for comparison (v0.1); a network-egress policy;
and diff-based edits (edit is full-file write for now). The brain also does not
review *successful* steps — re-engaging only on escalation is deliberate.

## Telemetry

Every model call records tokens, cost, and latency. Cost is OpenRouter's
**actual** per-generation cost: Relay sends `extra_body={"usage": {"include": True}}`
and reads the returned `response.usage` cost, falling back to `None` if OpenRouter
doesn't return one. This telemetry is the backbone of Relay's model-comparison
features in later milestones, so it's baked in from commit one.

## Install

```bash
pip install -e .
cp .env.example .env   # then add a key for the provider(s) you use
```

Relay is multi-provider. You only need the key for the provider(s) you actually
use: `OPENROUTER_API_KEY` (the default for both roles) and/or `DEEPSEEK_API_KEY`
(if a role uses `provider=deepseek`). With the defaults, an `OPENROUTER_API_KEY`
is all you need.

The `.env` is read from the **directory you run `relay` in** (the nearest `.env`
walking up from the current working directory), so per-project config works no
matter where Relay itself is installed. Real environment variables you export
override the file.

## Usage

```bash
# Show which model each role resolves to
relay models

# Run the brain → hands seam once for a goal
relay demo --goal "build a CLI todo app"

# Drive the two-role brain + hands loop against a goal (the default)
relay run --goal "create a file hello.txt containing the text: hi from relay"
relay run -g "add a hello route to a tiny flask app" --root .

# The interactive TUI: a two-pane chat over the same engine (type a goal to start)
relay tui                # first run with no key/config is guided into setup
relay tui --root . --assume 3   # in-app: ctrl+s opens the setup screen

# Configure providers/models/keys in the app (persisted globally; env still wins)
relay config show                                   # resolved config + key present/absent (never the key)
relay config set-role hands -p deepseek -m deepseek-v4-flash   # validated live before saving
relay config set-key deepseek                       # prompts WITHOUT echo; stored 0o600
relay config list-models deepseek                   # live /models (manual slug for openrouter)
relay config remove-key deepseek

# Preview: pause for approval after the brain produces the plan, before executing
relay run -g "refactor utils.py" --confirm-plan

# Single-model loop (no planner), for comparison/debugging
relay run -g "create hello.txt" --solo hands

# Unattended: auto-approve CONFIRM-category bash commands (BLOCKED still refused)
relay run -g "clean build artifacts" --auto-approve

# Preflight: check each role's (provider, model) resolves on its provider before
# it 404s mid-run; also shows the catalog source/status + per-role context window
relay doctor
relay doctor anthropic/claude-sonnet-4.5 openai/gpt-4o-mini   # probe slugs ad-hoc

# See recent runs (persisted to .relay/runs.jsonl); --no-log skips persistence
relay runs --limit 10
relay run -g "throwaway experiment" --no-log
```

`relay demo` asks the **brain** for one concrete next step and the **hands** how
they'd carry it out (a one-shot taste of the seam), then prints telemetry.

`relay run` (default) runs the **two-role** loop: the brain plans, the hands
execute each step in a narrow context, and the brain replans on escalation. It
streams the plan, each step + its result, any escalation + revised plan, and a
final terminal status, then prints the telemetry table **split brain vs hands**
(plus the parse-failure count). Options:

| Option | Default | Meaning |
| --- | --- | --- |
| `--goal` / `-g` | (required) | The goal to accomplish. |
| `--root` | `.` | Directory the tools are confined to. |
| `--solo <role>` | off | Run the single-model loop with that role instead of brain+hands. |
| `--confirm-plan` | off | Pause for approval after the plan, before execution. |
| `--auto-approve` / `-y` | off | Auto-approve `CONFIRM` bash commands (`BLOCKED` still refused). |
| `--max-steps` | `20` | Max model turns (solo mode only). |
| `--no-log` | off | Skip persisting this run to `.relay/runs.jsonl`. |

`relay run` fails gracefully with no API key, pointing you at `.env.example`.
When a `CONFIRM` command comes up without `--auto-approve`, it pauses and asks you
to approve or deny (see [Command policy](#command-policy-the-guardrail)). If
`--root` is a git repo with uncommitted changes, `relay run` prints a one-line
nudge to commit first (git is the real undo net — `bash` isn't sandboxed).

## Run history & preflight

Every `relay run` is persisted (unless `--no-log`) as one JSON line appended to
`<root>/.relay/runs.jsonl` — append-only, no schema migrations. This is the
durable floor the model **run-matrix** (a later milestone) will read; v0.05 only
records and displays individual runs, it does not sweep or rank model pairs.

```bash
relay runs              # a table of recent runs: when, mode, brain/hands models, status, cost, tokens, steps
relay runs --limit 25 --root path/to/project
```

`relay doctor` is a **preflight**: for each configured role (or any slugs you
pass), it makes a minimal `max_tokens=1` call through OpenRouter and reports
`OK` / `FAILED` with the reason — catching a retired-slug 404 ("no endpoints
found") *before* a real run depends on it. It exits non-zero if any slug failed
(usable in CI/scripts) and, with no `OPENROUTER_API_KEY`, says so and exits
rather than fabricating a result.

### The `runs.jsonl` schema

Each line is a `RunRecord` (`schema_version` lets future readers adapt):

```json
{
  "schema_version": 1,
  "run_id": "20260602T144107Z-ab12cd34",
  "timestamp": "2026-06-02T14:41:07+00:00",
  "goal": "create two files ...",
  "mode": "planned",
  "roles": {"brain": "anthropic/claude-sonnet-4.5", "hands": "anthropic/claude-3.5-haiku"},
  "status": "completed",
  "steps": 2,
  "escalations": 0,
  "parse_failures": 0,
  "per_role": [
    {"role": "brain", "model": "...", "calls": 1, "prompt_tokens": 367,
     "completion_tokens": 83, "total_tokens": 450, "cost_usd": 0.002346, "time_s": 4.48}
  ],
  "totals": {"tokens": 1470, "cost_usd": 0.003725, "time_s": 9.27},
  "wall_time_s": 9.4
}
```

`steps` is plan steps (planned) or executor turns (solo); `escalations` is
planned-only. `cost_usd` is `null` when OpenRouter didn't report a cost, and
`totals.cost_usd` sums only known costs. `wall_time_s` is real wall-clock,
distinct from the summed model latency in `totals.time_s`.

## Swapping models & providers

Each role names **both** a provider and a model, resolved from the environment —
no code change needed:

```bash
# Same provider (OpenRouter), different models:
export RELAY_BRAIN_MODEL="openai/gpt-4o"
export RELAY_HANDS_MODEL="anthropic/claude-3.5-haiku"

# Mixed providers — keep the planner on OpenRouter, run the executor on DeepSeek:
export RELAY_HANDS_PROVIDER="deepseek"
export RELAY_HANDS_MODEL="deepseek-v4-flash"   # use the v4 ids, not the legacy aliases

relay models
relay doctor   # confirm both roles resolve on their providers before spending
```

(Or set them in `.env`.) The provider defaults to `openrouter`; known providers
are `openrouter` and `deepseek`. Adding another OpenAI-compatible provider is a
one-line `ProviderProfile` in `relay/providers.py`.

**Thinking mode** is off by default and per-role: set `RELAY_HANDS_THINKING=1`
(or `RELAY_BRAIN_THINKING=1`) to opt a role in. It's off by default because
Relay's text protocol parses a single `content` blob — thinking mode splits
output into a separate `reasoning_content` stream Relay would discard, and some
multi-turn patterns 400 unless it's passed back; staying non-thinking sidesteps
that.

**The model catalog.** Pricing/capabilities/limits come from a catalog (default
`https://models.dev`), cached locally with a fresh→network→stale→bundled fallback
so cost is never silently zero. Power-user env overrides (rarely needed):
`RELAY_MODELS_URL` (source override), `RELAY_DISABLE_MODELS_FETCH` (cache/bundled
only), `RELAY_CACHE_DIR` (cache location). `relay doctor` prints which rung the
catalog resolved from.

## Develop

```bash
pip install -e ".[dev]"
scripts/test.sh            # the whole suite (recommended)
scripts/test.sh tests/test_tools.py   # forward any pytest args
```

Tests are network-free — the OpenAI-compatible client is mocked and the catalog
is served from a local fixture (`RELAY_MODELS_URL`) with the cache isolated to a
tmp dir, so the suite never makes a real API call.

**Run the suite via `scripts/test.sh`, not `pytest … | tail`.** Piping pytest
into `tail` (or `head`, etc.) makes the shell report *the pipe's last command's*
exit status — so a **red suite returns 0 and can be committed as green**. The
script runs pytest, prints a tail for a quick read, and then exits with *pytest's*
status, so a failure always propagates (even when its own output is piped). If you
invoke pytest directly and pipe it, use `set -o pipefail` (or check
`${PIPESTATUS[0]}`) so the failure isn't swallowed.

## Naming

The brand is **Relay** and the CLI command is `relay`. The PyPI distribution is
**`relay-cli`** because the bare `relay` name is already taken on the registries.
