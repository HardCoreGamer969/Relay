# Relay

> A coding agent built on a **planner/executor** architecture — a "brain" model
> that plans and a "hands" model that executes — with every model reached
> through **OpenRouter**.

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
(`max_executor_steps=12` per step, `max_escalations=3` replans, an optional
overall budget) so a weak model can't burn money in a spiral.

A planned run ends in one clear terminal status: `completed`, `planning_failed`,
`aborted_by_brain`, `escalation_limit`, or `max_steps`. Telemetry is recorded
per role, so the end-of-run table shows **brain vs hands** cost/tokens/time
separately — the seed of the later model bake-off. Run the single-model loop
instead with `relay run --solo hands`, or preview a plan before any writes with
`--confirm-plan`.

## Status — v0.04 (the brain/hands split)

The two-role architecture above is now live: `relay run` plans with the brain and
executes with the hands by default.

**What exists now:**

- `relay/client.py` — the one place that touches the OpenRouter (OpenAI-compatible) SDK.
- `relay/config.py` — role → model mapping (`brain`, `hands`) resolved from env.
- `relay/telemetry.py` — `CallRecord` / `Ledger` recording tokens, cost, latency, and parse-failure count, **split per role**.
- `relay/models.py` — `call_model(...)`, **the seam** everything else builds on.
- `relay/protocol.py` — the text action protocol + a tolerant `parse()` (now also `<plan>`/`<step>`, `<abort>`, `<blocked>`).
- `relay/policy.py` — the command policy: `classify()` → `BLOCKED` / `CONFIRM` / `ALLOW`.
- `relay/tools.py` — `read` / `list` / `grep` / `edit` / `bash`; `bash` consults the policy and an approver.
- `relay/loop.py` — `run_task(...)`, the single-model loop (kept for `--solo`; shares `execute_action`/`describe_action`).
- `relay/planner.py` — **the brain**: `make_plan(...)` and `replan(...)` (read-only investigation + planning).
- `relay/orchestrator.py` — **the two-role loop**: `run_planned(...)`, narrow executor context, bounded escalation.
- `relay/cli.py` — `relay models`, `relay demo`, and `relay run` (two-role by default; `--solo`, `--confirm-plan`, `--auto-approve`).
- Network-free tests for the protocol, tools, loop, policy, planner, and orchestrator.

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
<done>...short summary...</done>         ends the loop
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
cp .env.example .env   # then add your OPENROUTER_API_KEY
```

`.env.example` only needs an `OPENROUTER_API_KEY` — OpenRouter is the single
backend, so there are no other provider keys.

## Usage

```bash
# Show which model each role resolves to
relay models

# Run the brain → hands seam once for a goal
relay demo --goal "build a CLI todo app"

# Drive the two-role brain + hands loop against a goal (the default)
relay run --goal "create a file hello.txt containing the text: hi from relay"
relay run -g "add a hello route to a tiny flask app" --root .

# Preview: pause for approval after the brain produces the plan, before executing
relay run -g "refactor utils.py" --confirm-plan

# Single-model loop (no planner), for comparison/debugging
relay run -g "create hello.txt" --solo hands

# Unattended: auto-approve CONFIRM-category bash commands (BLOCKED still refused)
relay run -g "clean build artifacts" --auto-approve
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

`relay run` fails gracefully with no API key, pointing you at `.env.example`.
When a `CONFIRM` command comes up without `--auto-approve`, it pauses and asks you
to approve or deny (see [Command policy](#command-policy-the-guardrail)). If
`--root` is a git repo with uncommitted changes, `relay run` prints a one-line
nudge to commit first (git is the real undo net — `bash` isn't sandboxed).

## Swapping models

Models are resolved by role from the environment — no code change needed:

```bash
export RELAY_BRAIN_MODEL="openai/gpt-4o"
export RELAY_HANDS_MODEL="anthropic/claude-3.5-haiku"
relay models
```

(Or set them in `.env`.) Any OpenRouter model slug works.

## Develop

```bash
pip install -e ".[dev]"
pytest
```

Tests are network-free — the OpenRouter client is mocked, so `pytest` never
makes a real API call.

## Naming

The brand is **Relay** and the CLI command is `relay`. The PyPI distribution is
**`relay-cli`** because the bare `relay` name is already taken on the registries.
