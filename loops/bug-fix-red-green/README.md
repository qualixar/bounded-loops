# bug-fix-red-green

**Pattern:** evaluator-optimizer · **Role:** backend, engineering · **Rung:** L2 · **Gate:** pytest

The canonical bounded-loop example: drive an agent until a failing test passes.
This is the smallest self-contained "keep working until the test is green"
example: an agent proposes a fix and pytest alone decides when it is done.
It is also the template every other loop folder in this repo mirrors.

## What happens

A buggy `slugify.py` ships with one failing test (`test_multiple_spaces`).
The loop runs an agent against `PROMPT.md`, checks `pytest -q` after each lap,
and halts as soon as the gate is green. The agent cannot exit early by claiming success —
`pytest` is the ground truth.

## Prerequisites (one-time)

```bash
# Python 3.11+ and pytest — install once, not part of the timed run below.
pip install pytest
```

## Run it (keyless, <30s)

```bash
cd loops/bug-fix-red-green
./run.sh
```

`run.sh` checks for `pytest` on `PATH` first and fails fast with an install
hint if it's missing, rather than burning 15 silent iterations.

Expected output:
```
=== bug-fix-red-green — standalone run ===
--- Lap 1 ---
<promise>GREEN</promise>

GREEN — gate passed on lap 1
```

## Run it with the engine

```bash
# from repo root
pip install -e .
bl run loops/bug-fix-red-green
```

Expected:
```
✓ [DONE] gate-passed (laps: 1)  ledger: .../loops/bug-fix-red-green/.ledger.jsonl
Gate verified: the independent acceptance gate passed after 1 lap.
```

## Watch it wreck

```bash
./wreck.sh
```

By default this demonstrates the **LIE** failure mode: the stub never fixes
`seed/slugify.py`, but immediately claims `<promise>GREEN</promise>`. With no
gate, the loop believes it and exits early — the script's own diagnostic
epilogue then re-runs `pytest` independently and confirms the bug is still
there, exiting 1. Point `AGENT_CLI` at `cassettes/stub-agent.sh` instead to
see the truthful path, or at a real (misbehaving) agent to see drift and an
eventual **OVERRUN** (all 15 laps, no claim ever made).

> **Run `wreck.sh` on a clean `seed/`.** Unlike `bl run` (which operates on
> an isolated sandbox copy), the standalone `run.sh` fixes `seed/slugify.py`
> *in place*. So if you run `./run.sh` first and then `./wreck.sh` on the same
> clone, the bug is already fixed and the "lie" happens to become true — the
> contrast collapses. Reset with `git checkout -- seed/slugify.py` (or run
> `wreck.sh` on a fresh clone) before the wreck demo.

## Lift it into your own repo

1. Copy this folder.
2. Replace `seed/slugify.py` and `seed/test_slugify.py` with your target + test.
3. Edit `PROMPT.md` to describe your goal.
4. Run `./run.sh` to prove it works standalone, then `bl run` for the full engine.

## Which Anthropic pattern

`evaluator-optimizer` — the gate (pytest) is the evaluator; the agent-turn is the optimizer.
The loop runs until the evaluator says green.
Reference: [Anthropic — Building effective agents](https://www.anthropic.com/research/building-effective-agents)
