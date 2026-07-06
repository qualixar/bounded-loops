# type-annotations-present

**Role:** backend · quality · **Rung:** L1 · **Gate:** `command` (keyless) · **Runner:** stub (keyless)

A bounded loop that drives an agent until **every public top-level function
in a Python module fully annotates its parameters and return type**.

## What it demonstrates

The seed `api.py` has four top-level functions. Three are public and fully
annotated (`get_user`, `delete_user`, and one private `_internal_helper` that
is correctly out of scope). The fourth, `create_user`, is public but its
`name` parameter has no type annotation — an undocumented contract on a
function callers and static analyzers depend on.

The gate `seed/check_types.py` walks the module with `ast`, checks every
top-level public `def`/`async def`, and flags any parameter or return type
missing an annotation. The loop is DONE only when the checker exits 0.

## Run it (keyless, ~1s)

```bash
bl run loops/type-annotations-present --yes   # stub runner + real command gate
```

You'll see the ungated module fail the checker, the recorded fix add the
correct `str` annotation to `create_user`'s `name` parameter, then the gate
pass.

## Why the checker is `forbid:`-protected

The whole point is that the agent conforms the *module* to a real, honest
contract. Letting it "fix" the failure by editing the checker to skip
`create_user`, or by renaming the function to start with `_` to escape scope,
would fake a green gate — exactly the "agent talks its way past the
verifier" failure bounded-loops exists to prevent. The engine refuses any
write to `seed/check_types.py`.

## Make it real

Swap the stub runner for a real agent and swap the hand-rolled `ast` checker
for a real static type checker (`mypy --disallow-untyped-defs`,
`pyright --strict`) if you want full type-correctness checking beyond
presence-of-annotation. Keep the gate as the bottleneck: a public API is
never "done" until every signature is fully typed.
