# autogen-example

**Pattern:** evaluator-optimizer · **Role:** backend, engineering · **Rung:** L1 · **Gate:** pytest
**Runner:** `python_callable` — wires an EXISTING Microsoft Agent
Framework team into bounded-loops.

This is NOT a keyless demo like `bug-fix-red-green`. It assumes you already
have an Agent Framework agent/team and shows how to wire it into
bounded-loops' nine-bound safety net via `glue.py`'s
`run_turn(prompt, workspace) -> dict` contract. `bounded_loops` itself never
imports `agent_framework` — that discipline is the whole point of
`PythonCallableRunner`: one universal contract, not a bespoke adapter per
framework.

## A note on naming: "AutoGen" → "Microsoft Agent Framework"

AutoGen and Semantic Kernel converged into a single SDK, **Microsoft Agent
Framework**, in 2026. Verified directly against PyPI and the upstream
GitHub repo at implementation time (2026-07-05), not assumed from any prior
naming: the PyPI distribution name is **`agent-framework`**
(`pip install agent-framework`), the Python import root is
**`agent_framework`** (`from agent_framework import Agent`), and the project
lives at [github.com/microsoft/agent-framework](https://github.com/microsoft/agent-framework).
Current version at verification time: 1.10.0. If this loop stops working
against a future release, re-check `pypi.org/project/agent-framework/`
directly — this ecosystem has had naming churn and may again.

## Prerequisites (one-time)

```bash
pip install agent-framework
```

## What happens

A buggy `seed/app.py` ships with a failing `seed/test_app.py`. `glue.py`
constructs a real `agent_framework.Agent` standing in for "your existing
team," then performs a deterministic fix so the demo runs offline without
an LLM API key. Real usage: replace the deterministic `fix_bug()` call with
`await agent.run(prompt)` (with a configured chat client, e.g.
`OpenAIChatClient`) and let your LLM-backed agent reason about and edit the
file itself.

## Run it with the engine

```bash
# from repo root, with agent-framework installed
pip install -e .
bl run loops/autogen-example --yes
```

Expected:
```
✓ [DONE] gate-passed (laps: 1)  ledger: .../loops/autogen-example/.ledger.jsonl
Gate verified: the independent acceptance gate passed after 1 lap.
```

## `bl lint` works without Agent Framework installed

Manifest validation (`bl lint`) never imports the framework — it only
validates `loop.yaml`/`bounds.yaml` shape. This passes in CI even when
`agent-framework` isn't installed:

```bash
bl lint loops/autogen-example
```

## Known limitation: `changed` is hard-coded `True`

`glue.py`'s `run_turn` always returns `"changed": True`, regardless of
whether the agent actually modified anything. This is an honest, documented
simplification for demo purposes — it means bounded-loops' no-progress
bound (`bounds.no_progress_window`) can never fire for this example loop:
a framework call that silently does nothing would still report
`changed=True`. Production glue code should instead compare the target
file's content before and after the framework call and report the real
result (e.g. via a `git diff` check or a content hash comparison).

## Lift it into your own repo

1. Copy this folder.
2. Replace the `Agent` construction in `glue.py` with your own configured
   agent/team, and replace the deterministic `fix_bug()` call with
   `await agent.run(prompt)` once you have a chat client configured.
3. Replace `seed/app.py` / `seed/test_app.py` with your target + test.
4. Edit `PROMPT.md` to describe your goal.
5. Run `bl lint loops/autogen-example` first (keyless), then
   `bl run loops/autogen-example --yes` with `agent-framework` installed.

## Which Anthropic pattern

`evaluator-optimizer` — the gate (pytest) is the evaluator; the Agent
Framework agent invocation is the optimizer. The loop runs until the
evaluator says green.
Reference: [Anthropic — Building effective agents](https://www.anthropic.com/research/building-effective-agents)
