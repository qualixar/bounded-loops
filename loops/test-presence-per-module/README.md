# test-presence-per-module

**Role:** testing · **Rung:** L1 · **Gate:** `command` (keyless) · **Runner:** stub (keyless)

A bounded loop that drives an agent until **every source module has a
matching test file**. A module with zero test files isn't "untested in some
edge case" — it has literally no verified behavior at all, and that gap is
invisible in a normal `pytest -q` summary because there's no failing test to
show; there's just an absence.

## What it demonstrates

The seed has `seed/src/a.py` (an `add` function) and `seed/src/b.py` (a
`multiply` function). `seed/tests/test_a.py` exists and tests `add`;
`seed/tests/test_b.py` does not exist at all, so `multiply` has zero
verified coverage.

The gate `seed/check_test_presence.py` walks `seed/src/*.py` and checks that
each `<mod>.py` has a matching `seed/tests/test_<mod>.py`. The loop is DONE
only when the checker exits 0.

## Run it (keyless, ~1s)

```bash
bl run loops/test-presence-per-module --yes   # stub runner + real command gate
```

You'll see the ungated layout fail the checker on `b.py`, the recorded fix
write a real `tests/test_b.py` that imports and exercises `multiply`, then
the gate pass.

## Why the checker is `forbid:`-protected

The whole point is that the agent adds a genuine test for the *missing
module*. Letting it "fix" the failure by relaxing the checker's presence
rule (or by creating an empty placeholder file) would fake a green gate.
The engine refuses any write to `seed/check_test_presence.py`.

## Make it real

Swap the stub runner for a real agent and point the checker at your actual
`src/`/`tests/` layout, or run it as a pre-commit hook / CI gate ahead of
merge. Keep the gate as the bottleneck: a module lands only when it has at
least one real test exercising it.
