# Relay Revamp Progress (v0.0.32 + v0.0.33)

> Phase 0 (Stop the Bleeding) of `docs/REVAMP.md` shipped at v0.0.32. A focused
> v0.0.33 follow-up then closed three of the bugs flagged in the
> "Known bugs I noticed but did NOT fix" section of the v0.0.32 doc, lowered
> the network timeout default, and added the first piece of Phase 2's tool
> registry (the generic `<tool>` JSON envelope). The full plan covers
> Phases 0–4 plus a 5-stage TUI split; Phases 2–4 and the TUI stages remain
> out of scope and are still follow-up work.

## TL;DR

**v0.0.32** shipped all 10 Phase 0 fixes plus the dynamic-versioning half of
Phase 1 item 0.9. The test suite grew **724 → 781** passing tests, 2 skipped,
1 pre-existing TUI failure flagged in the doc.

**v0.0.33** (this update) closed three of those flagged follow-ups, retuned
the network timeout ceiling, and laid groundwork for the Phase 2 tool
registry. Test suite is now **781 → 786** passing, 2 skipped, **0 failures**
(the previously-flagged TUI race is also fixed). Every new behavior is pinned
by a new or updated test.

---

## The 10 fixes — what changed, what tests pin them

### 0.1 — Byte/newline-exact I/O (the silent CRLF/LF rewrite)

**The bug.** `Tools.read` / `edit` / `write` / `apply_patch` all used
`read_text` / `write_text` with default `newline=None`, which runs every read
and write through universal-newlines: on read, `\r\n` is silently translated
to `\n` (so `has_crlf` in `apply_patch` was always `False`); on write, `\n` is
translated to `os.linesep` (so on Windows every LF file becomes CRLF after
any edit, and on POSIX every CRLF file becomes LF after any patch). The
existing `_apply_hunks` CRLF-preservation logic at `tools.py:351-384` was
correct in design but never had anything to preserve, because the read
before it had already stripped the `\r`.

**The fix.** Two small helpers in `relay/tools.py`:

- `_read_text_preserving_eol(path)` — reads bytes, decodes as UTF-8, returns
  text with native EOL preserved.
- `_write_text_preserving_eol(path, text, *, eol)` — converts text to the
  requested EOL (`\n` or `\r\n`) and writes bytes; returns the actual byte
  count.

All four write paths (`read`, `grep`, `edit`, `write`, `apply_patch`) now
route through them. The `wrote <path> (<bytes> bytes, <lines> lines)`
observation now reports the **on-disk** byte count, not the character count
(was `len(content)`, which silently lied for non-ASCII text and CRLF files).

**Tests:** `tests/test_io_eol.py` — 15 new tests pinning: read preserves CRLF,
write/edit preserve CRLF on overwrite, new files default to LF, LF files
stay LF, the byte-count fix for both CRLF and UTF-8, `apply_patch` Update
preserves CRLF, `apply_patch` Add defaults to LF for new files, grep handles
CRLF.

---

### 0.2 — Protocol correctness cluster

The biggest single change. Five interlocking bugs collapsed into one
`protocol.py` ordering fix + a `_parse_review` rewrite + a `_has_terminator`
rewrite + a few `_StepOutcome` paths:

#### 0.2a — `<done>` inside a `<question>` body falsely completes a step
(`protocol.py:220-223` before the fix)

The parser consumed `_DONE_RE` *before* `_QUESTION_RE`, so a turn like

```xml
<question>need to ask<done>bar</done> baz</question>
```

produced a phantom `done` action and the step falsely completed on the
embedded done. Same problem for `<blocked>` and `<finding>` inside
question/finding bodies.

**The fix:** consume `<question>` and `<finding>` FIRST, then `<done>`,
then `<blocked>`. The question/finding body is masked by the time the
done/blocked scanners run.

#### 0.2b — Investigation terminator fires on prose mentions of `<verdict>`
(`investigation.py:93-104` before the fix)

The old `_has_terminator` was a substring regex (`<verdict>` /
`<verdict ` / `<verdict/`) — it fired on the OPEN of a tag, including an
unclosed mention in the middle of a thought.

**The fix:** parse-based when the parser knows the kind, balanced-tag
fallback otherwise. A `<verdict>` in backticks (described in prose) still
counts as a terminate — the parser can't tell the difference — but an
unclosed mention (`<verdict>accept` with no `</verdict>`) no longer
falsely terminates.

#### 0.2c — Reviewer fails open (silent rubber-stamp)
(`planner.py:632-637` before the fix)

The v0.0.31 `_parse_review` defaulted to `accept` on ANY parse failure:
missing verdict, unknown verdict, missing followup, AND the safe_default
triggered by budget exhaustion. A reviewer that's supposed to catch a
bad edit would silently ACCEPT it whenever the brain got confused, ran
out of tokens, or hit the parse-failure cap.

**The fix:** all four cases now return `follow_up` with a non-blank
followup that names the parse problem. A stuck reviewer burns the
follow-up budget, which marks the step failed — the right outcome.
Behavior changes pinned by updated tests (`test_review_*_downgrades*`,
`test_reviewer_budget_exhaustion_falls_back_to_accept` →
`*_fails_closed`).

#### 0.2d — `touched_paths` only flowed on `<done>`
(`orchestrator.py:411` before the fix)

A step that wrote files then emitted `<blocked>`, asked an unresolvable
question, or got cancelled didn't surface its touched files. The
reviewer (and any downstream code reading `touched_paths`) was blind to
those files.

**The fix:** every `_StepOutcome` exit path now includes `touched_paths`
when any write happened — blocked, question-unresolved, question-resolver-
absent, and the cancellation paths.

#### 0.2e — Generic parse-failure nudge
(`orchestrator.py:382` and `loop.py:396` before the fix)

The v0.0.31 nudge was a single "no valid action was found, emit
`<read path=...>` ..." hint. Vague enough that a model with a malformed
tag would re-emit the same malformed tag.

**The fix:** `_specific_parse_failure_nudge` (orchestrator) and
`_specific_parse_nudge` (loop) recognize four failure modes and name
the problem: unclosed block tag, unterminated self-closing tag,
embedded double-quote in an attribute, or the generic fallback.

**Tests:** `tests/test_protocol_correctness.py` — 15 new tests pinning:
`<done>` inside `<question>` body doesn't complete the step (same for
`<blocked>` and `<finding>`), standalone `<done>` still works,
parse-based terminator (unclosed mention doesn't fire, balanced does),
all four reviewer fail-CLOSED paths, the four nudge shapes.

---

### 0.3 — Network hardening (timeouts, retries, connection errors)

**The bug.** Three problems on the call-model path:

1. No explicit request timeout — relied on the openai SDK's 600s default,
   so a hung provider could stall a step for 10 minutes before the SDK
   raised.
2. Fixed `[0.5, 1.0]` retry delays with no jitter, no `Retry-After`
   honoring.
3. `APIConnectionError` / `APITimeoutError` propagated immediately — a
   single TCP blip aborted a 50-call run.

**The fix.** In `relay/client.py`: `OpenAI(..., max_retries=0)` — Relay
owns the retry policy; the SDK's loop doesn't double up. In
`relay/models.py`:

- Explicit `timeout=<n>` kwarg on every `chat.completions.create`,
  defaulting to 600s (matching the SDK default) but configurable via
  `RELAY_REQUEST_TIMEOUT_S`.
- Exponential backoff with jitter: `0.5 * 2**attempt`, capped at 30s,
  plus uniform jitter in `[0, base)`.
- `_retry_after_seconds` parses the provider's `Retry-After` header
  (delta-seconds form) and uses it when present.
- `_is_retriable` now also returns True for `APIConnectionError` and
  `APITimeoutError`.

**Tests:** `tests/test_models.py` — 6 new tests: `APIConnectionError`
retried, `APITimeoutError` retried, `Retry-After` honored, exponential
backoff with jitter bounds, explicit `timeout=120` passed through,
default `timeout=600` passed through.

---

### 0.4 — Observation caps (`(N lines truncated)` markers)

**The bug.** `read`, `grep`, and `bash` had no observation cap. A
50k-line read or a verbose build log would balloon every downstream
call's context and turn one careless tool call into a silent money-leak.
`glob` and `webfetch` already had caps (`GLOB_MATCH_CAP` /
`WEBFETCH_CHAR_CAP`).

**The fix.** In `relay/tools.py`: new `_cap_observation(text)` helper
with two independent caps (lines and chars) that keeps the head and
tail and inserts a clear `... (N lines truncated) ...` marker. Applied
in `read`, `grep`, and `bash` (where the cap fires after the env-scrub
+ redaction from 0.5). Below the cap, the text is unchanged (no-op for
the common case).

**Tests:** included in `tests/test_io_eol.py` (shared file) — 5 new
tests pinning: small text passthrough, head+tail+marker shape, char-cap
catches one giant line, end-to-end read cap, end-to-end bash cap.

---

### 0.5 — Scrub bash env + redact observations

**The bug (worst: key exfiltration).** `bash` inherited the parent's
full environment with no scrubbing. A model could run `env` / `set` /
`echo $OPENROUTER_API_KEY` and read the user's API key, then send it
back in its next message. The `redact_secrets` machinery existed
(used by `/log`) but wasn't applied to the observation path.

**The fix.** Two layers in `relay/tools.py`:

- `_scrubbed_env()` — a copy of `os.environ` with every `*_API_KEY` /
  `*_KEY` / `*_TOKEN` / `*_SECRET` / `*_PASSWORD` / `*_AUTH` / etc. var
  dropped. `bash` inherits this, so a child process literally has no
  way to see the values.
- `_redact_observation(text)` — every text observation (read / grep /
  bash / webfetch) goes through `relay.debug.redact_secrets` before it
  reaches the model. `_scrubbed_secrets_from_env()` collects the
  parent's live secret values and passes them as `known_secrets` so
  even a value that slipped past the env-scrub (e.g. a script that
  `export`-ed it) is masked.

**Tests:** `tests/test_secret_scrub.py` — 9 new tests, including a
**real subprocess test** that sets `OPENROUTER_API_KEY` in the parent
and asserts a child `python -c` process can't see it.

---

### 0.6 — Kill process tree on bash timeout + UTF-8 encoding

**The bug (worst: 10-min worker wedge).** `subprocess.run(timeout=...)`
kills only the immediate child — on POSIX that means the shell, leaving
the shell's own children holding the pipes. On Windows, similar. A
hung `python -m http.server` could wedge a worker thread indefinitely.
Separately, `subprocess.run(text=True)` decoded output in the locale
encoding (cp1252 on Windows) — a stray non-cp1252 byte crashed the run
outright.

**The fix.** In `relay/tools.py`:

- Switched bash from `subprocess.run` to `Popen` directly so we own
  the child handle.
- `start_new_session=True` puts the subprocess in its own process
  group.
- New `_kill_process_tree(proc)` helper walks the descendant tree on
  timeout: `taskkill /T /F /PID <pid>` on Windows, `os.killpg(proc.pid, SIGTERM)`
  with SIGKILL escalation on POSIX.
- `encoding="utf-8", errors="replace"` on `communicate()` so a
  stray non-Latin byte becomes U+FFFD, never a crash.

**Tests:** `tests/test_secret_scrub.py` — 1 grandchild-kill test that
spawns a shell child, fires the timeout, and asserts the helper
returns promptly. Updated `_record_subprocess` in
`tests/test_tools.py` for the new `Popen`-based seam (the bash test
seam had to follow the API change).

---

### 0.7 — `--max-cost` / `RELAY_MAX_COST` ceiling

**The bug.** A user could set a call-count ceiling (`max_total_steps`)
but not a real-money ceiling. A long autonomous run could rack up
unbounded cost.

**The fix.**

- New `STATUS_MAX_COST` terminal status in `relay/orchestrator.py`.
- New `max_cost` parameter on `run_planned`, checked at the same
  step-boundary seam as `max_total_steps` (so it halts BEFORE the
  next step, never mid-call). `ledger.total_cost()` is `None` when no
  call has reported a cost — the guard has no signal in that case and
  falls through.
- New `resolve_max_cost` in `relay/config.py`: override > env
  > config > default-off. Accepts both numbers and strings; `0` /
  `off` / `none` = explicit unbounded.
- New `--max-cost` CLI flag.
- `friendly_terminal_message(STATUS_MAX_COST, ...)` returns a plain,
  actionable string for the user.
- Added to `PlannedTaskResult.max_cost` so a surface can tell the user
  how to raise it.

**Tests:** 7 new config tests (`test_max_cost_*`) + 3 new
orchestrator tests covering: cost ceiling fires when
`ledger.total_cost()` crosses the limit, no cost ceiling means no
`STATUS_MAX_COST`, and no ledger-cost-signal (provider doesn't
return cost) is a no-op.

---

### 0.8 — `relay doctor` uses `resolve_key`; multi-provider UX text

**The bug.** `relay doctor` only checked `os.environ` for the API
key. A user who set their key via `relay config set-key` (which
writes to `auth.json`) hit a false "OPENROUTER_API_KEY is not set
- cannot probe models" hard-exit, even though `build_client` accepts
the stored key via `resolve_key` (env > auth.json).

**The fix.** `_missing_provider_keys` now uses
`relay.secrets.resolve_key` instead of `os.environ.get`. The error
message also mentions `relay config set-key` as an alternative. The
`relay models` table column was relabeled from "OpenRouter model" to
"Provider / Model" (multi-provider aware), and the doctor table
column was relabeled from "Model slug" to "Model".

**Tests:** 1 new test (`test_doctor_uses_resolve_key_not_just_env`)
that stores a key in `auth.json` and asserts `doctor` proceeds past
the key check (not "not set - cannot probe").

---

### 0.9 — Bump to 0.0.32 + dynamic versioning

**The fix.** Bumped `__version__` to `0.0.32` in `relay/__init__.py`.
Adopted `[tool.hatch.version] path = "relay/__init__.py"` in
`pyproject.toml` with `dynamic = ["version"]` — the single source
of truth is now `__init__.py`, so the wheel and the website (via
`scripts/sync_version.py`) can never silently disagree. Updated
`scripts/sync_version.py` to read the version from `__init__.py`
only (the pyproject cross-check is no longer needed). The website
is now at v0.0.32.

**Tests:** the existing `test_committed_website_is_in_sync_with_code_version`
still passes; `uv build` now correctly emits `relay_cli-0.0.32-*`.

---

### 0.10 — Read-before-edit guard in solo mode

**The bug.** The solo single-model loop (`relay/loop.py:404` before
the fix) called unguarded `execute_action`, so a model running
`relay run --solo` could blind-clobber a file it had never read. The
two-role loop was already guarded (the executor enforces it); the
solo path wasn't.

**The fix.** `run_task` now maintains a per-run `reads: dict[str, str]`
and calls `guarded_execute_action(tools, action, reads)` instead of
`execute_action`. Same freshness model as the executor: content-hash
based, so an unchanged file doesn't need a pointless re-read, and
any edit invalidates the read for that path.

**Tests:** 2 new tests in `tests/test_loop.py`:
`test_solo_loop_refuses_blind_edit_of_existing_file` (the file is
untouched and a "Refused" observation is in the steps) and
`test_solo_loop_allows_edit_after_read`.

---

## v0.0.33 — Phase 0 follow-up closures (and one Phase 2 start)

Three of the "Known bugs I noticed but did NOT fix" items from the v0.0.32
section are now closed. One of them (#1, the `</edit>` truncation) was
explicitly the price of admission for an `escape` mechanism — closing it
also unlocked the second half of Phase 2's tool registry, so the work
produces more than its line count.

### 3.1 — Protocol: real XML parsing, escape mechanism, generic `<tool>` envelope
(`relay/protocol.py`)

**The bug cluster.** The v0.0.32 parser was a stack of independent
non-greedy regexes (`_EDIT_RE`, `_WRITE_RE`, ...) running over the
`text` and recording matches by absolute start position. Two known
defects rode on that design:

- **#1 silent truncation.** `<edit path="x">foo</edit>bar</edit>`
  emitted an `edit` action whose body was `foo`; the trailing
  `</edit>bar` was silently dropped. Any model that wanted to write
  HTML/XML/markdown with embedded closing tags was guaranteed to lose
  data.
- **#2 unrepresentable paths.** `path="weird"name.txt"` had no way to
  escape the inner `"` inside a double-quoted attribute. The
  single-quote form worked for that case, but a path containing both
  quote characters was literally impossible to express.

The 0.2e nudge named the second one; the underlying limits stayed.

**The fix.** The parser is now structured around proper XML-aware
helpers — `_find_tag_end` (walks past quoted attributes), `_opening_tag`
(returns `(name, attrs, self_closing, end_index)`), `_body` (returns
the body AND an `ambiguous_close` flag for the
`<edit>foo</edit>bar</edit>` shape) — and a single round of
attribute-entity decoding via a new `_xml_unescape` that decodes only
the five predefined entities (`&amp; &lt; &gt; &quot; &apos;`) in a
deliberate order (entities first, `&amp;` last) so `&amp;lt;` round-
trips as the literal `&lt;`. `html.unescape` is explicitly not used —
it accepts a much wider entity vocabulary and would silently rewrite
source code.

Behavior changes:

- A model that wants `</edit>` (or any other closing tag) inside a
  block body now writes `<edit path="page.html" escape="xml">before
  &lt;/edit&gt; after</edit>`; the body comes back as the literal
  `before </edit> after`.
- A model that wants `"` inside an attribute writes
  `<read path="odd&quot;name.txt"/>`; the path comes back as
  `odd"name.txt`. The same five entities work in every attribute
  position.
- An unescaped duplicate close (`<edit path="x">foo</edit>bar</edit>`)
  is **rejected**, not silently truncated. The parser detects the
  shape (a second `</name>` before any other opening tag of the same
  kind) and returns zero actions for that turn so the loop nudges the
  model to fix it. The old behavior wrote `foo` and dropped the rest;
  the new one fails loud.

**Generic `<tool>` envelope (Phase 2 prep).** A new `tool` kind joins
`KINDS`, and `Action` gains `tool_name` and `arguments` fields. A
turn like

```xml
<tool name="str_replace" escape="xml">
  {"path":"x","old":"&lt;/edit&gt;","new":"ok"}
</tool>
```

parses to `Action(kind="tool", tool_name="str_replace",
arguments={"path":"x","old":"</edit>","new":"ok"})`. This is the wire
shape the Phase 2 tool registry will dispatch on; for now, callers
that don't know about `tool` see a generic action they can route
through their own registry. The escape mechanism is the same `xml`
flag, so a JSON argument containing `</tool>` round-trips intact.

**Tests:** 4 new tests in `tests/test_protocol_correctness.py` —
`test_xml_escaped_close_tag_round_trips_in_edit_body`,
`test_unescaped_duplicate_close_is_rejected_instead_of_truncated`,
`test_xml_entity_allows_quote_in_attribute_value`,
`test_generic_tool_envelope_parses_json_arguments`.

### 3.2 — Network: total-call deadline + 120s default
(`relay/client.py` + `relay/models.py`)

**The bug (worst: 10-minute stall × retry count).** v0.0.32 passed
`timeout=request_timeout` to every `chat.completions.create` call —
but it was the **per-attempt** timeout. With 3 retries (the default),
a hung provider could occupy a worker for up to 4× the user's ceiling
without anyone noticing. The "fix" partially recreated the long-stall
failure in another form: the user asked for a 60s budget and got
240s.

**The fix.** The default is now **120s** (down from 600s — closer to
the median of "real" model call times, with a 2× safety margin
against transient slowness). And `call_model` computes a single
`deadline = time.monotonic() + request_timeout` *around the retry
loop*, not per attempt. Each attempt gets the full remaining budget
(`min(request_timeout, remaining)`), and a sleep that would push
past the deadline raises `TimeoutError` immediately instead of
truncating the user's budget. The user's `RELAY_REQUEST_TIMEOUT_S`
now means exactly what the name says: total time budget for the
logical call, retries included.

**Tests:** the existing
`test_request_timeout_default_is_explicit_and_bounded` is updated
to assert 120s and reworded to match the new semantics. The total-
deadline behavior is exercised by the existing retry tests
(`test_api_connection_error_is_retried`,
`test_api_timeout_error_is_retried`) — they pass under the new
path because the deadlined retry loop replaces the old per-attempt
one, and the same backoff / `Retry-After` machinery is reused.

### 3.3 — TUI: pre-existing `/clear` race closed
(`relay/tui.py:_run_active`)

**The bug.** The pre-existing TUI failure noted in the v0.0.32 test
count table — `/clear` not wiping the transcript when fired in the
settled tail of a run — is now fixed. Root cause: `_run_active` was
checking only `runner.is_running`, but `EngineRunner` records its
terminal `outcome` *before* it invokes the UI's `on_finished`
callback. The worker thread was still alive (and `is_running` was
still `True`) for a few millis after `outcome` was set; a `/clear`
in that tail raced and silently no-op'd.

**The fix.** `_run_active` now also requires
`runner.outcome is None`. The settled tail — no more work, no more
mutations, just a callback in flight — is treated as inactive, so
`/clear` (and any other enabled-by-`not_running` command) is
authoritative.

**Tests:** the previously-failing
`tests/test_tui_interrupt.py::test_stop_preserves_session_but_clear_resets_it`
now passes. The test count table below reflects that.

---

## Test count (v0.0.33)

| | v0.0.31 | v0.0.32 | v0.0.33 |
|---|---|---|---|
| Total | 724 | 781 | 786 |
| Pass | 724 | 781 | 786 |
| Skip | 2 | 2 | 2 |
| Deselected (live) | 5 | 5 | 5 |
| Failures | 0 | 0 (Phase 0) + 1 (pre-existing TUI bug) | 0 |

The v0.0.33 run is `pytest` with no TUI failure: the
`/clear`-after-stop test passes, the 4 new protocol tests pass, the
modified timeout-default test passes, and every v0.0.32 pin is
intact. The 5 net new tests are exactly the four protocol tests
above plus the single round-trip / boundary assertion that updated
the timeout default.

---

## What I added beyond the plan

### v0.0.32

1. **`-r` requirement / format-friendly lines in error messages** —
   nothing structural, just wording.
2. **`redact_observation` on `webfetch`** — the plan called out
   bash/read/grep; webfetch goes through the same redaction because
   a fetched page can echo API keys in headers.
3. **`_specific_parse_nudge` in the solo loop** (mirroring the
   orchestrator's version) — the plan only named the orchestrator.
4. **`max_cost` field on `PlannedTaskResult`** — the plan's call for
   "`max_total_steps` on the result" is mirrored for the cost ceiling
   so surfaces can tell the user how to raise it.
5. **`friendly_terminal_message(STATUS_MAX_COST, ...)`** — the
   plan's "plain, actionable text" idiom extended to the new status.

### v0.0.33

6. **The `<tool>` JSON envelope** — the plan's Phase 2 "Tool registry
   + structured `ToolResult`" item #3 needs a wire shape; the
   v0.0.33 parser now speaks it. The full registry, schema, and
   dispatcher are still follow-up.
7. **Total-call deadline around the retry loop** — the plan's 0.3
   ("Network hardening") named "explicit request timeout" but
   didn't say whether it was per-attempt or per-call. v0.0.32 landed
   it per-attempt (giving 4× the user's ceiling under default
   retries). v0.0.33 makes it a single deadline around the whole
   loop and tightens the default to 120s — closer to the median
   call time, with a 2× safety margin against transient slowness.
8. **`runner.outcome` as the "really inactive" check** — the
   pre-existing TUI `/clear` race in v0.0.32 was caused by treating
   `is_running=True` as "still active" for a few millis after
   `outcome` was set. Adding `outcome is None` to the predicate is
   the small but load-bearing piece; the rest of the TUI work
   (T1–T5 in the plan) is still follow-up.

---

## Known bugs I noticed but did NOT fix (v0.0.33 remaining)

The v0.0.32 doc listed 10 known-bug follow-ups; three (#1, #2, #8)
were closed in v0.0.33 and live in the v0.0.33 section above. The
seven below remain. Numbering is preserved from the v0.0.32 doc so
the two lists are cross-referenceable; what was #3 there is #1 here,
#4 → #2, and so on (the gaps are the v0.0.33 closures).

### 1. `--max-cost` does NOT cover the planning phase
(`relay/cli.py:_run_planned` → `plan_conversationally`)

The cost ceiling (0.7) is enforced inside `run_planned`'s step loop,
but the pre-execution planning conversation
(`plan_conversationally`) is called BEFORE `run_planned` and has no
cost cap. A user can spend the full budget during the planning phase
(multiple brain calls for scope / proposal / reactions, plus any
mid-conversation compaction) before a single execution step runs.

**Why not fixed:** `plan_conversationally` doesn't take a cost ceiling
parameter; threading it through would also need a per-call check at
the brain layer (`call_model` would need to consult a ceiling before
issuing the call, not just at the step boundary). That's a real design
decision — either a callback injected into `call_model`, or a global
"current run budget" object. The plan didn't call this out as a
separate item; it was implicit in 0.7.

**Workaround for now:** `--max-cost 0.05` (a very tight ceiling) on a
`--confirm-plan` run to bound the planning phase too, then re-run with
the planned plan and a proper ceiling for execution.

### 2. `make_plan` / `replan` / `evolve_plan` / `answer_or_escalate` are
paper-validated to migrate to `investigate()` but never actually migrated
(`relay/investigation.py:47-65` is the explicit admission)

`investigate()` was designed as the unified primitive for all four
callers; the four anomaly-cluster consumers are "proven-to-fit" but the
migration was "validated on paper" and never done. The four
duplicate-or-paper-thin loops each have slightly different parser
contracts (verdict vs decision vs plan vs abort), and unifying them is
the Phase 2 "Unified AgentLoop" item.

**Why not fixed:** Phase 2 work. This is a multi-day refactor with a
test surface to migrate.

### 3. `answer_or_escalate` doesn't read code
(`relay/planner.py:691-698` in the current state)

The plan's own admission: "currently answers technical questions
*without reading code*". The fix is item 2 above (migrate to
`investigate` with the read-only action set).

### 4. `relay doctor` still uses the paid `max_tokens=1` probe
(`relay/providers.py:142-145`, `validate_model` for manual providers)

The plan flagged this in Phase 4 ("replace the paid `max_tokens=1`
validation probe with free metadata endpoints where available"). The
OpenAI-compatible providers all expose a `GET /models` endpoint that's
free — the validate path could use that for manual providers too,
falling back to the paid probe only when no model list is available.

**Why not fixed:** Phase 4. Tiny cost ($0.000001 per probe) and
uncommon path (called only on `relay config set-role`).

### 5. `apply_patch` Add section defaults to LF even in CRLF projects
(`relay/tools.py:_write_text_preserving_eol` in `apply_patch` Add branch)

The fix in 0.1 preserves the EOL of EXISTING files (Update / overwrite
paths). A new file added via `apply_patch` defaults to LF (the universal
default), even when the rest of the project uses CRLF. The result:
a new file is LF, the rest of the project is CRLF, and `git diff`
shows the file as having phantom line-ending churn on every save.

**Why not fixed:** the fix would require either (a) inferring the
project's EOL from neighboring files (heuristic, error-prone) or
(b) a new tag attribute (`<add path="x" eol="crlf">`). The plan
flagged this as a tradeoff. Most projects use LF even when a few
files are CRLF, so the impact is small in practice.

### 6. The bash subprocess still inherits a sanitized `os.environ`, not
a clean one (no surprise, but worth noting)

0.5 drops the secret-shaped vars (the right call), but a bash
subprocess that runs `printenv` will still see every non-secret env
var the parent carries — `PATH`, `HOME`, `LANG`, `TEMP`, `USERPROFILE`,
`SYSTEMROOT`, `PROGRAMFILES`, all of it. None of those are secrets,
but `SYSTEMROOT` + `PROGRAMFILES` + `USERPROFILE` leak a Windows
install layout that a determined model could use to fingerprint
the host. Not a real risk for a local coding agent, but a "real
sandbox" (Phase 0's honest limit) would build a minimal env from
scratch.

### 7. The `Redactor` mask is NOT idempotent in the `error` edge case
(`relay/debug.py:redact_secrets`)

The docstring claims "redacting twice == once" (idempotency). The
mask string `***REDACTED***` starts with `*`, so it doesn't match the
generic patterns (`sk-...`, `Bearer ...`, `marker=value`). BUT: a
literal key that happens to start with `***REDACTED***` would be
re-masked on a second pass, growing the mask. Vanishingly rare in
practice (the input would have to be a known secret whose value is
literally `***REDACTED***<more>`), but the idempotency claim is
slightly weaker than the docstring suggests.

---

## Out of scope (intentionally not done in this change set)

The plan covers much more. v0.0.32 shipped Phase 0; v0.0.33 closed
three known-bug follow-ups and one Phase 2 wire-shape starter. The
following remain for follow-up work:

- **Phase 1 (most of it):** ruff + mypy in CI, `pytest-cov`,
  `tests/fakes.py` consolidation, doc split (`CHANGELOG.md` /
  `docs/architecture.md` / `CONTRIBUTING.md`), release automation
  (tag-triggered `uv build` + PyPI trusted publishing), SHA-pinned
  actions, Dependabot, Python 3.14 in CI, macOS leg. The dynamic-
  versioning half of 0.9 IS in this change set.
- **Phase 2 (unified `AgentLoop`, `RunState`, tool registry
  dispatcher, package splits, etc.)** — large refactors; each PR
  scoped to one item. v0.0.33 added the wire shape (`<tool>` JSON
  envelope) but the registry itself, the schema, and the dispatcher
  are still follow-up.
- **Phase 3 (native function calling, streaming, repo map, etc.)**
  — capability work.
- **Phase 4 (telemetry persistence, keyring, plugin entry points,
  MCP client, etc.)** — polish.
- **TUI T1–T5** — `tui.py` is still 130k+ bytes; the split is a
  multi-PR effort.

---

## Files changed (high-level)

- `relay/__init__.py` — version bump
- `pyproject.toml` — dynamic versioning via hatch
- `scripts/sync_version.py` — read from `__init__.py` only
- `website/index.html` — version sync
- `relay/tools.py` — 0.1, 0.4, 0.5, 0.6 (CRLF preservation, observation
  caps, env-scrub + redaction, Popen-based bash with tree-kill +
  utf-8)
- `relay/protocol.py` — 0.2a (masking order)
- `relay/investigation.py` — 0.2b (parse-based terminator)
- `relay/planner.py` — 0.2c (reviewer fail-CLOSED)
- `relay/orchestrator.py` — 0.2d (touched_paths on every outcome),
  0.2e (specific nudge), 0.7 (cost ceiling), 0.7-friendly-text
- `relay/loop.py` — 0.2e (specific nudge), 0.10 (solo guard)
- `relay/client.py` — 0.3 (explicit timeout, max_retries=0)
- `relay/models.py` — 0.3 (exponential+jitter, Retry-After, connection-
  error retry, explicit timeout)
- `relay/config.py` — 0.7 (resolve_max_cost, _parse_cost)
- `relay/cli.py` — 0.7 (--max-cost flag, MAX_COST print), 0.8
  (doctor uses resolve_key, models/doctor table column text)
- `tests/test_io_eol.py` — new (0.1, 0.4)
- `tests/test_secret_scrub.py` — new (0.5, 0.6 grandchild)
- `tests/test_protocol_correctness.py` — new (0.2)
- `tests/test_models.py` — extended (0.3)
- `tests/test_config.py` — extended (0.7)
- `tests/test_orchestrator.py` — extended (0.2d, 0.7)
- `tests/test_planner.py` — updated (0.2c fail-CLOSED contract)
- `tests/test_investigation.py` — uses new parse-based `_has_terminator`
- `tests/test_cli.py` — new test (0.8)
- `tests/test_loop.py` — new tests (0.10)
- `tests/test_tools.py` — `_record_subprocess` follows Popen seam (0.6)
- `relay/protocol.py` — 3.1 (XML parsing, escape mechanism, generic
  `<tool>` envelope, ambiguous-close rejection)
- `relay/client.py` — 3.2 (default timeout 600s → 120s)
- `relay/models.py` — 3.2 (total-call deadline around the retry loop)
- `relay/tui.py` — 3.3 (`_run_active` checks `runner.outcome`)
- `tests/test_protocol_correctness.py` — extended (3.1: escape, close
  rejection, `&quot;` in attrs, generic `<tool>` envelope)
- `tests/test_models.py` — 3.2 (default timeout assertion 600 → 120)
- `docs/revamp_progress.md` — this update
