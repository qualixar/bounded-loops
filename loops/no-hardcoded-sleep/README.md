# no-hardcoded-sleep

**Role:** testing · **Rung:** L1 · **Gate:** `command` (keyless) · **Runner:** stub (keyless)

A bounded loop that drives an agent until **no test contains a hardcoded
`time.sleep(...)` call**. A guessed-duration sleep is the classic
flaky-and-slow anti-pattern: too short under load (flaky), too long in the
common case (slow), and it silently accumulates across a growing suite.

## What it demonstrates

The seed `sample_tests.py` has three tests: `test_addition` and
`test_subtraction` (both clean), and `test_worker_finishes`, which starts a
worker then calls `time.sleep(5)` before asserting it's done — instead of
polling the already-available `worker_is_done(handle)` helper.

The gate `seed/check_sleep.py` parses the file with `ast` and flags any call
to `time.sleep(...)` (or `sleep(...)` via `from time import sleep`). The
loop is DONE only when the checker exits 0.

## Run it (keyless, ~1s)

```bash
bl run loops/no-hardcoded-sleep --yes   # stub runner + real command gate
```

You'll see the ungated file fail the checker on the `time.sleep(5)` call,
the recorded fix replace it with a poll loop on `worker_is_done`, then the
gate pass.

## Why the checker is `forbid:`-protected

The whole point is that the agent removes the guessed wait from the *test*.
Letting it "fix" the failure by renaming the import to dodge the AST match,
or by weakening the checker's detection, would fake a green gate. The
engine refuses any write to `seed/check_sleep.py`.

## Make it real

Swap the stub runner for a real agent and point the checker at your actual
test suite, or run it as a pre-commit hook / CI gate. Keep the gate as the
bottleneck: a test lands only when it waits on a real signal, not a guess.
