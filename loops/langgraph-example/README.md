# langgraph-example

**Pattern:** evaluator-optimizer · **Role:** backend, engineering · **Rung:** L1 · **Gate:** pytest
**Runner:** `python_callable` — wires an EXISTING LangGraph graph into bounded-loops.

This is NOT a keyless demo like `bug-fix-red-green`. It assumes you already have
a compiled LangGraph graph and shows how to wire it into bounded-loops' nine-bound
safety net via `glue.py`'s `run_turn(prompt, workspace) -> dict` contract.
`bounded_loops` itself never imports `langgraph` — that discipline is the whole
point of `PythonCallableRunner`: one universal contract, not a bespoke adapter
per framework.

## Prerequisites (one-time)

```bash
pip install langgraph
```

## What happens

A buggy `seed/app.py` ships with a failing `seed/test_app.py`. `glue.py` builds
a trivial single-node LangGraph graph (`edit_node`) that reads `seed/app.py`,
fixes the bug, and writes it back — standing in for "your existing graph."
Real usage: replace the graph construction in `glue.py` with your own
compiled graph; only the `workspace` file I/O is bounded-loops-specific.

## Run it with the engine

```bash
# from repo root, with langgraph installed
pip install -e .
bl run loops/langgraph-example --yes
```

Expected:
```
✓ [DONE] gate-passed (laps: 1)  ledger: .../loops/langgraph-example/.ledger.jsonl
Gate verified: the independent acceptance gate passed after 1 lap.
```

## `bl lint` works without LangGraph installed

Manifest validation (`bl lint`) never imports the framework — it only
validates `loop.yaml`/`bounds.yaml` shape. This passes in CI even when
`langgraph` isn't installed:

```bash
bl lint loops/langgraph-example
```

## Known limitation: `changed` is hard-coded `True`

`glue.py`'s `run_turn` always returns `"changed": True`, regardless of
whether the LangGraph graph actually modified anything. This is an honest,
documented simplification for demo purposes — it means bounded-loops'
no-progress bound (`bounds.no_progress_window`) can never fire for this
example loop: a framework call that silently does nothing would still
report `changed=True`. Production glue code should instead compare the
target file's content before and after the framework call and report the
real result (e.g. via a `git diff` check or a content hash comparison).

## Lift it into your own repo

1. Copy this folder.
2. Replace the graph construction in `glue.py` with your own compiled
   LangGraph graph.
3. Replace `seed/app.py` / `seed/test_app.py` with your target + test.
4. Edit `PROMPT.md` to describe your goal.
5. Run `bl lint loops/langgraph-example` first (keyless), then
   `bl run loops/langgraph-example --yes` with `langgraph` installed.

## Which Anthropic pattern

`evaluator-optimizer` — the gate (pytest) is the evaluator; the LangGraph
graph invocation is the optimizer. The loop runs until the evaluator says
green.
Reference: [Anthropic — Building effective agents](https://www.anthropic.com/research/building-effective-agents)
