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

## Status — v0.02 (tools + text protocol + single-model agent loop)

This is the milestone where Relay stops *describing* work and starts *doing* it.
A single model drives a loop: it emits actions as plain-text tags, Relay parses
and executes them with real tools, feeds the results back, and repeats until the
goal is met.

**What exists now:**

- `relay/client.py` — the one place that touches the OpenRouter (OpenAI-compatible) SDK.
- `relay/config.py` — role → model mapping (`brain`, `hands`) resolved from env.
- `relay/telemetry.py` — `CallRecord` / `Ledger` recording tokens, cost, latency, **and parse-failure count**.
- `relay/models.py` — `call_model(...)`, **the seam** everything else builds on.
- `relay/protocol.py` — the text action protocol + a tolerant `parse()`.
- `relay/tools.py` — `read` / `list` / `grep` / `edit` / `bash`, confined to a project root.
- `relay/loop.py` — `run_task(...)`, the single-model agent loop.
- `relay/cli.py` — `relay models`, `relay demo`, and **`relay run`**.
- Network-free tests for the protocol, tools, and loop.

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

**Intentionally NOT here yet** (later milestones): the brain/hands planner split
(v0.02 runs **one** role only), diff-based edits (edit is full-file write for
now), and any command guardrails — `bash` has **minimal safety only** (it refuses
paths outside the project root, but has no command denylist or confirmation
policy). That guardrail layer is v0.03.

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

Both commands fail gracefully with no API key, pointing you at `.env.example`.

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
