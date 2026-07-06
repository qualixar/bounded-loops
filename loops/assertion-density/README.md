# assertion-density

**Role:** testing · **Rung:** L1 · **Gate:** `command` (keyless) · **Runner:** stub (keyless)

A bounded loop that drives an agent until **every `test_*` function
contains at least one real assertion**. A test with no assert is a special
kind of lie: it runs, it's green, it shows up in the coverage report — and
it can never fail no matter how broken the code under test becomes.

## What it demonstrates

The seed `sample_tests.py` has three tests: `test_addition` (asserts
correctly), `test_login_flow` (calls `login(...)`, prints the result, but
never asserts anything — passes even if `login` always returns `False`), and
`test_login_rejects_empty_password` (asserts correctly).

The gate `seed/check_assertions.py` parses the file with `ast` and flags any
`test_*` function with zero `assert` statements. The loop is DONE only when
the checker exits 0.

## Run it (keyless, ~1s)

```bash
bl run loops/assertion-density --yes   # stub runner + real command gate
```

You'll see the ungated file fail the checker on `test_login_flow`, the
recorded fix add a real assertion on the login result, then the gate pass.

## Why the checker is `forbid:`-protected

The whole point is that the agent adds a genuine check to the *test*.
Letting it "fix" the failure by weakening the checker (e.g. accepting a
bare `pass` or a tautological assert as sufficient) would fake a green gate.
The engine refuses any write to `seed/check_assertions.py`.

## Make it real

Swap the stub runner for a real agent and point the checker at your actual
test suite, or run it as a pre-commit hook / CI gate ahead of merge. Keep
the gate as the bottleneck: a test lands only when it can actually fail.
