# adk-example

**Pattern:** evaluator-optimizer · **Role:** backend, engineering · **Rung:** L1 · **Gate:** pytest
**Runner:** `python_callable` — wires an EXISTING Google Agent
Development Kit (ADK) `LoopAgent` into bounded-loops.

This is NOT a keyless demo like `bug-fix-red-green`. It assumes you already
have an ADK `LoopAgent`/`Runner` setup and shows how to wire it into
bounded-loops' nine-bound safety net via `glue.py`'s
`run_turn(prompt, workspace) -> dict` contract. `bounded_loops` itself
never imports `google.adk` — that discipline is the whole point of
`PythonCallableRunner`: one universal contract, not a bespoke adapter per
framework.

## Verified package name (2026-07-05)

Verified directly against PyPI and the upstream GitHub repo at
implementation time, not assumed from any prior naming: PyPI distribution
name is **`google-adk`** (`pip install google-adk`), Python import root is
**`google.adk`** (`from google.adk.agents import LoopAgent`), maintained at
[github.com/google/adk-python](https://github.com/google/adk-python).
Current version at verification time: 2.3.0.

### Naming-churn note found during verification

`LoopAgent` is **deprecated** as of ADK 2.0 in favor of `Workflow` — the
class still exists and works today (confirmed against the real
`google/adk-python` source), but upstream marks it
`@deprecated('LoopAgent is deprecated ... Please use Workflow instead.')`
and states it will be removed in a future version. This loop still uses
`LoopAgent` because it matches this repo's own frozen vocabulary
(`LoopAgent(max_iterations/escalate, deterministic)`), but
a real deployment against a future ADK release should re-check
[pypi.org/project/google-adk/](https://pypi.org/project/google-adk/) and
consider migrating this glue to `Workflow`.

## Prerequisites (one-time)

```bash
pip install google-adk
```

## What happens

A buggy `seed/app.py` ships with a failing `seed/test_app.py`. `glue.py`
constructs a real `google.adk.agents.LoopAgent` (with `max_iterations=1`
and empty `sub_agents`, standing in for "your existing LoopAgent"), then
performs a deterministic fix so the demo runs offline without a
`session_service` or LLM API key. Real usage: give the `LoopAgent` real
LLM-backed `sub_agents` and drive it via `google.adk.Runner` plus a
configured `session_service`, per ADK's real async execution model.

## Run it with the engine

```bash
# from repo root, with google-adk installed
pip install -e .
bl run loops/adk-example --yes
```

Expected:
```
status: DONE  laps: 1  ledger: loops/adk-example/.ledger.jsonl
```

## `bl lint` works without ADK installed

Manifest validation (`bl lint`) never imports the framework — it only
validates `loop.yaml`/`bounds.yaml` shape. This passes in CI even when
`google-adk` isn't installed:

```bash
bl lint loops/adk-example
```

## Known limitation: `changed` is hard-coded `True`

`glue.py`'s `run_turn` always returns `"changed": True`, regardless of
whether the LoopAgent actually modified anything. This is an honest,
documented simplification for demo purposes — it means bounded-loops'
no-progress bound (`bounds.no_progress_window`) can never fire for this
example loop: a framework call that silently does nothing would still
report `changed=True`. Production glue code should instead compare the
target file's content before and after the framework call and report the
real result (e.g. via a `git diff` check or a content hash comparison).

## Lift it into your own repo

1. Copy this folder.
2. Replace the `LoopAgent` construction in `glue.py` with your own
   sub-agents, and drive it via `google.adk.Runner` + a real
   `session_service` once you have one configured (or migrate to
   `Workflow`, per the deprecation note above).
3. Replace `seed/app.py` / `seed/test_app.py` with your target + test.
4. Edit `PROMPT.md` to describe your goal.
5. Run `bl lint loops/adk-example` first (keyless), then
   `bl run loops/adk-example --yes` with `google-adk` installed.

## Which Anthropic pattern

`evaluator-optimizer` — the gate (pytest) is the evaluator; the ADK
`LoopAgent` invocation is the optimizer. The loop runs until the evaluator
says green.
Reference: [Anthropic — Building effective agents](https://www.anthropic.com/research/building-effective-agents)
