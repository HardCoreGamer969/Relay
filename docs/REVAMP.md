# Relay Revamp Plan

> Produced 2026-07-08 from a five-track deep analysis of the repo (core engine,
> tools/safety layer, provider/config infrastructure, TUI, and the
> tests/CI/docs/website quality stack). Every finding cited below carries a
> `file:line` reference valid as of commit `ad36ecf`.

> **Product differentiation** (bake-off, assumption profiles, model router, etc.)
> lives in [`features/MASTER.md`](features/MASTER.md) — a sibling roadmap, not a
> section of this file. This plan stays engineering: bugs, structure, parity
> hygiene. When a features-doc item needs infra from here, link it; do not
> duplicate designs across the two trees.

---

## The verdict: don't rewrite — refactor in place

Relay's architecture is worth keeping. Several things here are genuinely
better than typical home-grown agents:

- **Narrow executor context** (`orchestrator.py:261-305`) — the hands never
  sees the full plan or the brain's reasoning. This is real context
  engineering and directly enables cheap hands models.
- **Bounded everything** — every loop has an explicit budget
  (`orchestrator.py:583-589`); cancel unblocks parked asks so workers are
  always joinable (`bridge.py:200-209`).
- **The read-before-edit guard** (`loop.py:219-314`) — content-hash
  freshness, per-section patch coverage, recoverable refusals. Correct.
- **`bridge.py` in full** — UI-framework-free, first-settle-wins `UiRequest`,
  headless-testable. The best-designed module in the repo.
- **The test suite** — 678 hermetic tests faked at exactly the right seam
  (the OpenAI-client boundary), CI-enforced network freedom, a budget-capped
  live canary. This is the safety net that makes the refactor below safe.
- **Honesty discipline** — module docstrings state their own limits
  (`tools.py:6-12`, `policy.py:1-24`). Preserve this voice.

What's wrong is fixable in place: confirmed bugs, heavy duplication, two
monolith files (`tui.py` 2,862 lines; `cli.py` 917 lines), and a generation
gap versus modern agents (no native tool calling, no streaming, no resume).

---

## Confirmed bugs (verified during analysis)

1. **The v0.0.31 CRLF-preservation code is dead, and line endings are
   silently rewritten on every edit.** `Tools.read` uses `read_text` with
   default `newline=None` (`tools.py:445`), so universal newlines strips
   `\r\n` before `has_crlf` is checked (`tools.py:352`) — always False.
   `write_text` with `newline=None` (`tools.py:530,545,601,614`) translates
   `\n` → `os.linesep`: on Windows every LF file becomes CRLF after any
   write; on POSIX every CRLF file becomes LF after any patch. Latent
   corruption: if `has_crlf` ever were True, Windows writes produce `\r\r\n`.
   All three behaviors verified empirically.
2. **The step reviewer fails open.** Budget exhaustion or an unparseable
   verdict defaults to *accept* (`planner.py:632-637`), and the
   investigation terminator fires on prose mentions of `<verdict>`
   (`investigation.py:93-104`) — a silent rubber-stamp path.
3. **`<done>` inside a `<question>` body falsely completes a step** —
   `_DONE_RE` is consumed before `_QUESTION_RE` (`protocol.py:220-223`).
   Same class: an `<edit>` body containing the literal `</edit>` truncates
   silently (`protocol.py:112`); paths containing `"` are unrepresentable
   (`protocol.py:104`).
4. **`relay doctor` bypasses `auth.json`** — `_missing_provider_keys` checks
   only `os.environ` (`cli.py:551`) while `build_client` uses `resolve_key`
   (env > auth.json). Keys saved via `relay config set-key` produce a false
   "not set — cannot probe" hard exit.
5. **Version drift shipped to main** — `pyproject.toml:7` and
   `relay/__init__.py:264` say 0.0.31 while main's latest work is labeled
   v0.0.32; the website badge lags too.
6. **Key-exfiltration path** — bash inherits the full parent env with no
   scrubbing (`tools.py:741-748`) and no output redaction, so `env`/`set`
   sends `OPENROUTER_API_KEY` into an observation POSTed to the provider.
   The redactor exists (`debug.py:70-98`) but is used only for `/log`.
7. **Bash timeout doesn't kill the process tree** — `subprocess.run(timeout=…)`
   kills only the shell; grandchildren survive holding the pipes and the
   post-kill drain can block forever (`tools.py:741-748`). Output decodes in
   the locale encoding (cp1252 on Windows) — mojibake or `UnicodeDecodeError`.
8. **No request timeout on model calls** — `build_client` passes only
   `base_url`/`api_key` (`client.py:40`), so the OpenAI SDK's 600s default
   applies; a hung provider stalls a step 10 minutes. Retries are fixed
   `[0.5, 1.0]` with no jitter/`Retry-After`, and connection errors are never
   retried (`models.py:36-66`).

---

## Phase 0 — Stop the bleeding (~1 week; all S/M; each independently shippable)

| # | Fix | Effort |
|---|-----|--------|
| 0.1 | Byte/newline-exact I/O: `newline=""` (or bytes) at `tools.py:445,530,545,581,601,614`; preserve per-file EOL for real; report true on-disk bytes | S |
| 0.2 | Protocol correctness cluster: mask `<question>`/`<finding>` bodies before `<done>`/`<blocked>`; parse-based (not substring) terminators in `investigate`; reviewer fails **closed** (flagged follow-up, never silent accept); return `touched_paths` on all `_StepOutcome` paths; emit specific malformed-tag feedback instead of the generic nudge | S |
| 0.3 | Network hardening: explicit request timeout; use `openai.OpenAI(max_retries=…)` or a proper backoff wrapper (exponential + jitter, honor `Retry-After`, retry connection errors); deconflict with the hand-rolled loop | M |
| 0.4 | Cap read/grep/bash observations (head+tail with `(N lines truncated)` markers), consistent with glob/webfetch | S |
| 0.5 | Scrub bash env (`*_API_KEY` etc.) and run every observation through `redact_secrets` before it reaches the model | S |
| 0.6 | Kill the whole process tree on bash timeout (Job Objects / `taskkill /T` on Windows; `start_new_session` + `killpg` on POSIX) + `encoding="utf-8", errors="replace"` | M |
| 0.7 | `--max-cost` / `RELAY_MAX_COST` ceiling checked against `ledger.total_cost()` at the step-ceiling seam | S |
| 0.8 | Fix `doctor` to use `resolve_key`; fix multi-provider UX text (provider-aware error panels, drop the "OpenRouter model" column title) | S |
| 0.9 | Bump to 0.0.32 everywhere, then adopt hatchling dynamic versioning (`[tool.hatch.version] path = "relay/__init__.py"`) so drift is impossible | S |
| 0.10 | Apply the read-before-edit guard to solo mode (`loop.py:404` currently calls unguarded `execute_action`) | S |

## Phase 1 — Quality rails (parallel with Phase 0; mostly S)

- **Lint/format/types**: add `[tool.ruff]` (pinned, chosen rules), make
  `ruff check` + `ruff format --check` blocking (remove `continue-on-error`
  from `quality.yml:78`); add mypy or pyright with a permissive baseline and
  ratchet — the codebase is fully annotated but nothing verifies it.
- **Coverage**: `pytest-cov` in one CI matrix cell with a modest fail-under.
  Known blind spots: no `test_telemetry.py`, no `test_secrets.py`.
- **Release automation**: tag-triggered workflow → `uv build` → PyPI trusted
  publishing; add `pip install relay-cli` docs. Closes the half-finished
  packaging story.
- **Docs split**: extract the ~590-line "Status" devlog (README:51-643) into
  `CHANGELOG.md`; move the architecture explanation to
  `docs/architecture.md`; add `CONTRIBUTING.md` (test.sh, TTY_COMPATIBLE,
  live-tier rules). README shrinks to pitch/install/usage.
- **Test hardening**: consolidate the copy-pasted fakes
  (`_resp`/`ScriptedClient`/`RoutedClient`/`_ArcClient`) into
  `tests/fakes.py`; route fakes on **exported marker constants** instead of
  prompt substrings (today one prompt reword silently breaks dozens of
  tests); move `--allow-hosts` into pyproject addopts; set
  `TTY_COMPATIBLE=0` in `tests/conftest.py`.
- **Supply chain**: SHA-pin third-party actions (especially
  `peaceiris/actions-gh-pages`, which holds `contents: write`); add
  Dependabot for pip + actions; add Python 3.14 and one macOS CI leg;
  consider PR-based website version sync instead of `[skip ci]` pushes to
  main.

## Phase 2 — Structural refactor (incremental; ordered by dependency)

The core problem: **the model-loop is written five times** (`run_task`,
`_run_executor_step`, `make_plan`, `replan`, `evolve_plan`) when
`investigate()` (`investigation.py:106`) was purpose-built to unify them —
`investigation.py:47-65` admits the migration was validated on paper and
never done.

1. **Unified `AgentLoop`** (M) — generalize `investigate()` to support
   read-only *and* write action sets, terminators, budgets, and a shared
   parse-failure policy; migrate all five copies (plus `answer_or_escalate`,
   which currently answers technical questions *without reading code*,
   `planner.py:691-698`). Wire or delete the dead `ParseFailureTracker`
   (`loop.py:32-70`). This is the enabling refactor for everything below.
2. **Extract `RunState` from `run_planned`** (L) — replace the 460-line
   closure state machine (`orchestrator.py:569-1029`) with an explicit state
   object serialized at step boundaries to `.relay/sessions/<id>/`. Every
   constituent (Plan, PlanMemory, MemoryBus, Transcript) already has
   `to_state()` wired to nothing — this is mostly assembly. Deliverables:
   crash-safe runs, `relay resume`, a testable engine.
3. **Tool registry + structured `ToolResult`** (M) — one registry (name,
   schema, executor fn, description) as the single source of truth, rendered
   to the tag prompt today and native tool schemas in Phase 3. Replace
   string-prefix sniffing (`startswith("wrote ")`,
   `orchestrator.py:448-451`) with `ToolResult(ok, kind, summary, payload)`;
   `apply_patch` raises like every other tool.
4. **Package splits** (M each): `relay/tui/` (see the TUI plan below);
   `cli.py` → commands + a shared `relay/render.py` + `relay/doctor.py`
   (killing the tui→cli private-import cycle at `tui.py:2544-2549`); one
   typed settings layer replacing the five hand-rolled precedence ladders
   (`config.py:37-207,309-334`, `context.py:48-78`) with `.env.example`
   generated from the schema; config-driven provider registry with
   capability-keyed cost extraction (`providers.py:54`,
   `models.py:200-202`).
5. **Consolidation sweep** (S) — one event type (three incompatible callback
   shapes exist today); enums for the stringly-typed statuses; merge
   `EngineRunner._run`/`_run_steer`; delete dead code (`_confirm_plan_gate`,
   `OPENROUTER_BASE_URL`, the write-orphaned `POOL_HANDS` — implement
   Stage 2 or remove it); move the changelog out of `__init__.py`.

## Phase 3 — Modernization (the capability gap)

1. **Native function calling with the tag protocol as fallback** (L) — the
   catalog already tracks `tool_call` capability per model
   (`catalog.py:92`). Normalize provider `tool_calls` and parsed tags into
   the same `Action` stream. Keeps the "every model works" moat, eliminates
   the tag-escaping/parse-failure bug class for capable models, unlocks
   parallel tool calls.
2. **Streaming end-to-end** (L) — streaming `call_model`, incremental
   tag/tool-call parser, mid-generation cancellation (the money-leak guard
   currently stops before the *next* call, not the current one), live tokens
   in the TUI.
3. **Editing upgrade** (M) — `str_replace` tool (unique-match old/new, clear
   multi-match errors) + line-numbered `read` with offset/limit; demote
   whole-file `edit`; fix `apply_patch`'s no-forward-cursor and first-match
   bugs (`tools.py:317-326,361-364`) which can silently edit the wrong
   duplicate block.
4. **Enforce context windows on the live message lists** (M) — the MemoryBus
   budgeting guards the minority of tokens; raw observations accrete
   unbounded within a step (`orchestrator.py:340-346,464-466`). Cap per
   observation at insertion; fold older turns via the existing compaction
   machinery when nearing the resolved window.
5. **Permission layer** (L) — per-tool policies (read=allow,
   write=confirm-with-diff, bash=classify, webfetch=domain allowlist +
   private-IP block), per-project config, session "always allow", diff
   previews in approval prompts; close the `bash -c`/interpreter-one-liner
   policy holes; SSRF guard for webfetch.
6. **Session persistence + resume** (M) — builds directly on Phase 2's
   `RunState`; TUI picker in the plan below.
7. **Repo map + retrieval** (M) — tree-sitter/ctags symbol map replacing the
   filename-only `project_digest` (`planner.py:133-168`); token-based BM25
   replacing substring matching (`memory.py:405-410`); line-boundary
   trimming in `fit_to_budget` (`_utils.py:27`).
8. **Repo-aware, bounded search + background bash** (M) — honor
   `.gitignore`, skip `.git`/binaries, cap matches, lazy iteration
   (`tools.py:477`), optionally shell out to ripgrep; managed
   background-process tool (start/poll/kill) for dev-server workflows.

## Phase 4 — Differentiation

- **Model bake-off telemetry**: add `provider` to `CallRecord`
  (`telemetry.py:16-27`), persist per-call records (only aggregates survive
  today), build the run-matrix comparison the telemetry docstring promises.
- **Keyring-backed secrets** on Windows (the advertised `0o600` is a no-op
  there, `secrets.py:31-33`) with `auth.json` fallback; replace the paid
  `max_tokens=1` validation probe with free metadata endpoints where
  available.
- **De-rot the catalog**: generate `_BUNDLED_RAW` at release time from a
  models.dev snapshot; memo TTL (`catalog.py:429-439`); catalog-driven role
  defaults (the pinned default already 404'd once, `config.py:232-233`).
- **Plugin entry points** for providers; structured logging
  (`RELAY_LOG`/`--verbose`); MCP client (optional, after native tool
  calling).

---

# TUI Revamp Plan (full)

The TUI is the product surface, and it deserves its own plan. Today
`relay/tui.py` is 2,862 lines; `RelayTuiApp` alone spans
`tui.py:1131-2862` — ~1,730 lines and ~90 methods owning view, controller,
and chunks of model. The revamp is five stages, each shippable on its own.
The 18 `test_tui_*.py` files (120 tests) are the safety net — they pin
behavior through injectable seams, so a mechanical split does not require
rewriting them.

## What to preserve verbatim

- **`relay/bridge.py` in full** — `EngineBridge`, `UiRequest` (first-settle-
  wins), `InputRouter` (one input box, seven meanings), `Session` with the
  completed-run-only cwd adoption guard. Do not touch it; every stage below
  works against its existing contract.
- **The single-stream layout** — conversation, live in-place plan block
  (◉/◍/○ updated in place, `tui.py:1980-2031`), tool lines, findings,
  verdicts interleaved in event order. This is the shape modern agent TUIs
  converged on and Relay's brain/hands/findings attribution on top of it is
  a differentiator.
- **The palette + discipline** — brain=magenta, hands=cyan, findings=green,
  you=bright magenta; "no CRT/scanlines; motion only where there is
  activity" (`tui.py:341-343`); the splash and de-robotified voice.
- **The "zero new tokens" rendering invariant** —
  `describe_event_for_activity` (`tui.py:369-421`) is pure presentation of
  already-emitted events. Keep it a hard rule: the TUI never spends money.
- **Interrupt semantics** — esc=interrupt→steer-or-stop, `/redirect` at the
  clean boundary, `/queue` FIFO, plan-revision budget.
- **`present_prompt` as the single prompt chokepoint** (`tui.py:222-229`)
  and the redacted `/log` export path.

## Stage T1 — Split `tui.py` into a package (L; the enabler; no behavior change)

Mechanical extraction with the bridge contract untouched:

```
relay/tui/
  __init__.py      # re-export RelayTuiApp for cli.py's lazy import
  app.py           # RelayTuiApp: compose, lifecycle, _marshal, quit (~300 lines)
  theme.py         # palette + CSS. Dedupe #06090e/#080d14 (today in the
                   # palette constant AND literal CSS, tui.py:344,1137,1158)
                   # into Textual CSS variables; one stylesheet, not three
                   # embedded strings (tui.py:480-491, 781-793, 1136-1174)
  stream.py        # StreamView widget: _push_row/_write_* line writers, the
                   # in-place plan block (_plan_*), mirror buffers
                   # (from tui.py:1875-2052)
  events.py        # describe_event_for_activity, _render_event,
                   # _SPECIAL_EVENTS (tui.py:369-421, 1677-1736) — SHARED
                   # with cli.py's _print_event so the two renderers can't
                   # drift (today cli.py:837 duplicates the mapping)
  commands/        # registry.py (Command dataclass + COMMANDS) + one module
                   # per family (config, ops), operating on a narrow
                   # AppServices protocol instead of private app._cmd_*
                   # (from tui.py:647-711, 1099-1128, 2318-2724)
  dialogs.py       # SelectDialog, TextEntryDialog, SegmentedControl,
                   # FilterInput (tui.py:769-1094) — with ONE shared
                   # highlight/filter core (today implemented three times:
                   # popover tui.py:1482-1531, SelectDialog tui.py:828-894,
                   # SegmentedControl tui.py:1038-1083)
  input.py         # PromptInput + popover controller + history recall
                   # (tui.py:713-766, 1482-1531)
  setup.py         # SetupScreen (tui.py:468-644)
  controller.py    # run lifecycle: _start_run/_start_steer deduped into one
                   # _launch_runner (today 14 identical kwargs twice,
                   # tui.py:1566-1580 vs 1609-1623), interrupt fork, queue
                   # consumption, cost accounting (tui.py:1549-1836)
  status.py        # status line + placeholders + mode LED (tui.py:2088-2249)
```

Layering fixes that ride along:

- Move `persist_role` (writes config.json from the view, `tui.py:442-465`)
  into `relay/config.py`; move `friendly_provider_error` into
  `relay/providers.py`; move `_save_key`/`_live_key_values` (actual API-key
  strings resolved inside the view file, `tui.py:2473-2480, 2792-2805`) out
  of the TUI entirely.
- Extract `_doctor_checks`/`_build_provider_clients`/`_run_doctor` from
  cli.py into `relay/doctor.py` — kills the tui→cli private-import inversion
  (`tui.py:2544-2549` importing cli privates while `cli.py:818` lazily
  imports the TUI).
- Move business decisions off the view: cost folding (`tui.py:1749-1753`),
  steer-budget enforcement (`tui.py:1592-1599`), queue consumption policy
  (`tui.py:1828-1836`), interrupt-fork routing (`tui.py:1756-1772`) belong
  on `Session`/the controller.

Exit criteria: all 120 TUI tests green with only import-path updates; no
rendered-output diffs (the mirror buffers make this assertable).

## Stage T2 — Foundation fixes (M; correctness + felt latency)

1. **Move every network call off the UI thread.** `SetupScreen.compose`
   currently does a live provider HTTP call during compose
   (`tui.py:534,546-561`); `persist_role` live-validates synchronously on
   Enter (`tui.py:456` via 581/2448); `/model` list fetch and `/doctor`'s
   per-role probes freeze the app for seconds (`tui.py:2366-2370,
   2539-2549`). Wrap them in Textual `@work(thread=True)` workers with
   spinner states in the dialogs. The injectable seams
   (`list_models_fn`/`validate_fn`/`doctor_fn`) already exist, so tests
   don't change. This is the single biggest felt-latency fix.
2. **Bounded, virtualized stream.** Every stream line mounts a fresh
   `Static` into a `VerticalScroll` with no cap (`tui.py:1901-1904`) — long
   sessions degrade layout and memory, and the mirror buffers grow forever.
   Replace with Textual `RichLog` (`max_lines`) or a custom Line-API widget;
   cap the mirror buffers; make `scroll_end` conditional on "was already at
   bottom" (today `tui.py:1897` yanks the reader to the bottom on every
   mount — scrollback reading during a run is impossible).
3. **Harden the thread boundary.** Give `Ledger` and `Transcript` explicit
   locks or snapshot methods — the UI thread currently reads
   worker-mutated state relying on CPython list-append atomicity
   (`tui.py:1860, 2224`). Log, don't swallow, exceptions in `_marshal`
   (`tui.py:1634-1636` silently loses events today, including
   `on_finished`). Replace the 0.2s transcript poll (`tui.py:1305`) with a
   `transcript_turn` event or a worker→UI queue — one delivery path instead
   of polling + events both running.
4. **Unify slash-command dispatch.** One router where every command accepts
   optional inline args (an `accepts_args` field on `Command`) — today
   `/queue foo` works typed but `/model gpt-4o` does not because inline
   parsing is special-cased for exactly two commands (`tui.py:1445-1451`).
5. **Kill string round-trips and bare-except noise.** Pass structured
   `(speaker, phase, text)` into the stream instead of re-parsing
   `format_turn` output (`tui.py:1926-1933`); classify verdicts from event
   payload fields, not `"accept" in verdict.lower()` (`tui.py:1968`); narrow
   the 31 `except Exception` guards to `NoMatches`/teardown checks with a
   debug log so real render bugs surface.

## Stage T3 — Rendering upgrade (M; the visible gap vs Claude Code/OpenCode)

1. **Markdown rendering for brain turns.** `Markdown`/`RichLog`/`Syntax`/
   `TextArea` are never imported today — model output renders as flat text
   rows. Render brain/conversation turns through Textual `Markdown` (or Rich
   `Markdown` renderables in the stream): code blocks, lists, and headings
   in model output finally look like something.
2. **Syntax-highlighted diffs and file previews.** Tool calls currently
   render as a one-line label + 60-char result (`tui.py:1950-1957`) — the
   user never sees what an edit actually changed. Render diff/code-shaped
   observations through Rich `Syntax` / a diff renderable. When Phase 3's
   permission layer lands, the same renderable powers approve-with-diff.
3. **Collapsible tool output.** Replace the hard 200/60-char truncations
   (`tui.py:1700, 1956`) with a fold: collapsed summary line +
   keybinding/click to expand. Pairs with the virtualized stream from T2.
4. **Theme object.** One `Theme` with Textual CSS variables; palette defined
   once; groundwork for user theming later.

## Stage T4 — Interaction upgrade (M each)

1. **Multi-line composer.** Swap `PromptInput(Input)` for a
   `TextArea`-based composer — Enter=submit, Shift+Enter=newline — keeping
   the popover and history routing. The multi-line paste fix
   (`tui.py:717-739`) currently preserves pasted newlines *invisibly* inside
   a one-line widget; this makes them editable.
2. **A real permission prompt.** Replace y/n-through-the-shared-input
   approval (`tui.py:134`; prompt built at `bridge.py:189-196`; fixed
   yes-set at `bridge.py:68` that silently denies "sure"/"yep") with a
   dedicated modal: the command verbatim in a styled panel, the policy
   reason, and `[1] approve once · [2] approve for session · [3] deny`. The
   bridge needs no change — the modal calls `request.deliver("yes"/"no")`;
   the session allowlist is UI-side state handed into `run_kwargs`. Shows
   the T3 diff renderable for write-approval when the permission layer
   lands.
3. **Session persistence + resume UI.** Serialize `Session` (transcript,
   memory, queue, cwd, costs — `bridge.py:448-457`) to `.relay/sessions/`,
   autosaved at run boundaries (`_handle_finished`). Turn `/runs` (today a
   read-only list with no action on select, `tui.py:2555-2573`) into a
   session picker whose selection restores the stream from the transcript.
   Builds on Phase 2's `RunState`; the highest-value new capability in the
   whole TUI plan.
4. **Context + streaming indicators.** A context-window usage gauge next to
   the cost readout (the engine already resolves per-role windows in
   `context.py`); once Phase 3 streaming lands, tokens render live into the
   stream and the status LED distinguishes "model is generating" from
   "tool is running".

## Stage T5 — Polish (S each; ongoing)

- Clickable file paths in tool lines (open-in-editor), click-to-expand
  folds, mouse-friendly dialogs.
- Scrollback search (`/find`-style) and copy-mode/export-visible-region.
- User theming + keybinding configuration surface.
- Rotate the mirror-buffer testing pattern toward a render-model layer: a
  pure "list of stream entries" model that both the widget and headless
  tests consume, so display refactors can't corrupt the `/log` export
  (which is currently load-bearing on the test buffers,
  `tui.py:2756-2764`).

## TUI sequencing

> **Product cockpit plan (locked decisions + U0–U6):**  
> [`docs/features/tui-revamp/MASTER.md`](features/tui-revamp/MASTER.md)  
> folds T1–T5 into stages U0–U6 with IDE density, website brand, and anim kill switch.
> Prefer that folder for status; this section remains the engineering rationale.

```
T1 (split)  ──►  T2 (foundations)  ──►  T3 (rendering)  ──►  T5 (polish)
                        │
                        └──►  T4.1/T4.2 (composer, permission modal)
                              T4.3 (resume) ── after Phase 2 RunState
                              T4.4 (streaming UI) ── after Phase 3 streaming
```

T1 is a prerequisite for everything and is pure mechanics. T2 items are
independent of each other. T3 depends on T2's virtualized stream. T4.3 and
T4.4 are the only items gated on engine work.

---

## Overall cadence

- **Release 1 (call it 0.1.0 — it's earned)**: Phase 0 + Phase 1.
- **Release 2**: Phase 2 items landed one PR at a time behind the test
  suite, plus TUI T1+T2.
- **Release 3**: Phase 3 native tool calling + streaming (built once against
  Phase 2's registry/settings seams), TUI T3+T4.
- **Ongoing**: Phase 4 and TUI T5.
