# crewai-example

**Pattern:** evaluator-optimizer · **Role:** backend, engineering · **Rung:** L1 · **Gate:** pytest
**Runner:** `python_callable` — wires an EXISTING CrewAI crew into bounded-loops.

This is NOT a keyless demo like `bug-fix-red-green`. It assumes you already have
a CrewAI crew (`Agent`/`Task`/`Crew`) and shows how to wire it into bounded-loops'
nine-bound safety net via `glue.py`'s `run_turn(prompt, workspace) -> dict`
contract. `bounded_loops` itself never imports `crewai` — that discipline is the
whole point of `PythonCallableRunner`: one universal contract, not a bespoke
adapter per framework.

## Prerequisites (one-time)

```bash
pip install crewai
```

## What happens

A buggy `seed/app.py` ships with a failing `seed/test_app.py`. `glue.py`
constructs a real CrewAI `Agent`/`Task`/`Crew` (`Process.sequential`) standing
in for "your existing crew," then performs a deterministic fix so the demo
runs offline without an LLM API key. Real usage: replace the deterministic
`fix_bug()` call with `crew.kickoff()` and let your LLM-backed agent reason
about and edit the file itself.

## Run it with the engine

```bash
# from repo root, with crewai installed
pip install -e .
bl run loops/crewai-example --yes
```

Expected:
```
✓ [DONE] gate-passed (laps: 1)  ledger: .../loops/crewai-example/.ledger.jsonl
Gate verified: the independent acceptance gate passed after 1 lap.
```

## `bl lint` works without CrewAI installed

Manifest validation (`bl lint`) never imports the framework — it only
validates `loop.yaml`/`bounds.yaml` shape. This passes in CI even when
`crewai` isn't installed:

```bash
bl lint loops/crewai-example
```

## Known limitation: `changed` is hard-coded `True`

`glue.py`'s `run_turn` always returns `"changed": True`, regardless of
whether the crew actually modified anything. This is an honest, documented
simplification for demo purposes — it means bounded-loops' no-progress bound
(`bounds.no_progress_window`) can never fire for this example loop: a
framework call that silently does nothing would still report
`changed=True`. Production glue code should instead compare the target
file's content before and after the framework call and report the real
result (e.g. via a `git diff` check or a content hash comparison).

## Lift it into your own repo

1. Copy this folder.
2. Replace the `Agent`/`Task`/`Crew` construction in `glue.py` with your own
   crew, and replace the deterministic `fix_bug()` call with
   `crew.kickoff()` once you have an LLM API key configured.
3. Replace `seed/app.py` / `seed/test_app.py` with your target + test.
4. Edit `PROMPT.md` to describe your goal.
5. Run `bl lint loops/crewai-example` first (keyless), then
   `bl run loops/crewai-example --yes` with `crewai` installed.

## Which Anthropic pattern

`evaluator-optimizer` — the gate (pytest) is the evaluator; the CrewAI crew
invocation is the optimizer. The loop runs until the evaluator says green.
Reference: [Anthropic — Building effective agents](https://www.anthropic.com/research/building-effective-agents)
