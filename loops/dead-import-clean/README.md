# dead-import-clean

**Role:** backend · quality · **Rung:** L1 · **Gate:** `command` (keyless) · **Runner:** stub (keyless)

A bounded loop that drives an agent until **a Python module imports nothing
it doesn't use**.

## What it demonstrates

The seed `module.py` has three working functions (`order_total`,
`most_common_item`, `to_json`) with real logic, plus six imports. Four are
used (`json`, `math`, `Counter`, `Iterable` — the last one in a type hint,
which the checker correctly counts as a reference); two are dead leftovers
from a refactor (`os`, `sys`).

The gate `seed/check_imports.py` walks the module with `ast`, collects every
bound import name, and flags any that never appear as a `Name`/`Attribute`
load anywhere else in the file. The loop is DONE only when the checker exits
0.

## Run it (keyless, ~1s)

```bash
bl run loops/dead-import-clean --yes   # stub runner + real command gate
```

You'll see the ungated module fail the checker, the recorded fix strip the
dead imports while keeping every function's logic intact, then the gate pass.

## Why the checker is `forbid:`-protected

The whole point is that the agent conforms the *module* to real usage.
Letting it "fix" the failure by editing the checker to stop flagging unused
imports would fake a green gate — exactly the "agent talks its way past the
verifier" failure bounded-loops exists to prevent. The engine refuses any
write to `seed/check_imports.py`.

## Make it real

Swap the stub runner for a real agent and swap the hand-rolled `ast` checker
for a real linter (`pyflakes`, `ruff --select F401`) if you want full
coverage (star-imports, conditional imports, `__all__` re-exports). Keep the
gate as the bottleneck: a module is never "done" until every import earns its
place.
