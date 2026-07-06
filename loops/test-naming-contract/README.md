# test-naming-contract

**Role:** testing · **Rung:** L1 · **Gate:** `command` (keyless) · **Runner:** stub (keyless)

A bounded loop that drives an agent until **every test-like function is
named `test_*`**, so a pytest-style runner actually collects it. Naming
contracts are the easiest way for a test to silently rot: rename `test_foo`
to `check_foo` during a refactor and pytest just stops running it — no
failure, no signal, just quiet coverage loss.

## What it demonstrates

The seed `sample_tests.py` has three functions: `test_subtraction` (correctly
named), `check_addition` (asserts `add(2, 2) == 4` but is NOT collected by
pytest because it doesn't start with `test_`), and `TestMath.test_add_negative`
(correctly named method on a `Test*` class).

The gate `seed/check_test_names.py` parses the file with `ast` and flags any
function that looks like a test — a method on a `Test*` class, or a
module-level function containing an `assert` — that isn't named `test_*`.
The loop is DONE only when the checker exits 0.

## Run it (keyless, ~1s)

```bash
bl run loops/test-naming-contract --yes   # stub runner + real command gate
```

You'll see the ungated file fail the checker on `check_addition`, the
recorded fix rename it to `test_addition`, then the gate pass.

## Why the checker is `forbid:`-protected

The whole point is that the agent conforms the *test file* to the naming
contract. Letting it "fix" the failure by weakening the checker (e.g.
dropping the assert-detection heuristic) would fake a green gate. The engine
refuses any write to `seed/check_test_names.py`.

## Make it real

Swap the stub runner for a real agent and point the checker at your actual
test suite (or run it as a pre-commit hook / CI gate). Keep the gate as the
bottleneck: a rename lands only when every previously-passing test is still
named so the runner keeps collecting it.
