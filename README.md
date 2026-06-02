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

## Status — v0.03 (the command-policy guardrail)

Relay can drive a loop and *do* work (v0.02); v0.03 adds the seatbelt: a command
policy that classifies every `bash` command before it runs — refusing the
catastrophic ones outright and pausing the destructive-but-legitimate ones for
approval — before the loop becomes more autonomous in later milestones.

**What exists now:**

- `relay/client.py` — the one place that touches the OpenRouter (OpenAI-compatible) SDK.
- `relay/config.py` — role → model mapping (`brain`, `hands`) resolved from env.
- `relay/telemetry.py` — `CallRecord` / `Ledger` recording tokens, cost, latency, **and parse-failure count**.
- `relay/models.py` — `call_model(...)`, **the seam** everything else builds on.
- `relay/protocol.py` — the text action protocol + a tolerant `parse()`.
- `relay/policy.py` — **the command policy**: `classify()` → `BLOCKED` / `CONFIRM` / `ALLOW`.
- `relay/tools.py` — `read` / `list` / `grep` / `edit` / `bash`; `bash` now consults the policy and an approver.
- `relay/loop.py` — `run_task(...)`, the single-model agent loop (now threads the approver).
- `relay/cli.py` — `relay models`, `relay demo`, and `relay run` (now with `--auto-approve`).
- Network-free tests for the protocol, tools, loop, and policy.

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

**Intentionally NOT here yet** (later milestones): the brain/hands planner split
(Relay still runs **one** role only — v0.04), process/container **sandboxing** of
`bash` (v0.95), a network-egress policy, and diff-based edits (edit is full-file
write for now).

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

# Drive the single-model agent loop against a goal (this one actually does work)
relay run --goal "create a file hello.txt containing the text: hi from relay"
relay run -g "add a docstring to main.py" --root . --max-steps 20 --role hands

# Unattended: auto-approve CONFIRM-category bash commands (BLOCKED still refused)
relay run -g "clean build artifacts" --auto-approve
```

`relay demo` asks the **brain** for exactly one concrete next step, hands that
step to the **hands** to describe how they'd carry it out, then prints a
telemetry table (tokens / cost / time per role, with a total).

`relay run` drives the agent loop: the model emits an action, Relay executes it
with the tools, streams the action and a result snippet to the console, and
repeats until the model emits `<done>` (or it hits `--max-steps`). At the end it
prints the telemetry table **plus the parse-failure count**. Options:

| Option | Default | Meaning |
| --- | --- | --- |
| `--goal` / `-g` | (required) | The goal to accomplish. |
| `--root` | `.` | Directory the tools are confined to. |
| `--max-steps` | `20` | Maximum model turns before stopping. |
| `--role` | `hands` | Which single role drives the loop. |
| `--auto-approve` / `-y` | off | Auto-approve `CONFIRM` bash commands (`BLOCKED` still refused). |

Both commands fail gracefully with no API key, pointing you at `.env.example`.
When a `CONFIRM` command comes up without `--auto-approve`, `relay run` pauses and
asks you to approve or deny it (see [Command policy](#command-policy-the-guardrail)).

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
