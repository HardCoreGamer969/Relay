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

## Status — v0.01 (the model layer)

This first milestone ships **only the model layer**: the single seam every later
part of Relay is built on, plus telemetry on every call.

**What exists now:**

- `relay/client.py` — the one place that touches the OpenRouter (OpenAI-compatible) SDK.
- `relay/config.py` — role → model mapping (`brain`, `hands`) resolved from env.
- `relay/telemetry.py` — `CallRecord` / `Ledger` recording tokens, cost, and latency.
- `relay/models.py` — `call_model(...)`, **the seam** everything else builds on.
- `relay/cli.py` — `relay models` and `relay demo`.
- Network-free tests.

**Intentionally NOT here yet** (later milestones): tools, an agent loop, bash
execution, file editing, guardrails, and any real planner/executor decomposition
logic. The `demo` command calls each role exactly once — nothing more.

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
```

`relay demo` asks the **brain** for exactly one concrete next step, hands that
step to the **hands** to describe how they'd carry it out, then prints a
telemetry table (tokens / cost / time per role, with a total). With no API key it
fails gracefully and points you at `.env.example`.

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
