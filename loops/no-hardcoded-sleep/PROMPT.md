# No Hardcoded Sleep: remove guessed-duration waits from tests

Goal: make `python3 seed/check_sleep.py seed/sample_tests.py` report that no
test contains a hardcoded `time.sleep(...)` call (exit 0).

Steps each turn:
  1. Run the checker:
     `python3 seed/check_sleep.py seed/sample_tests.py`
  2. For each call it flags, open `seed/sample_tests.py` and replace the
     `time.sleep(...)` line with a real completion check: call the
     already-available `worker_is_done(handle)` helper directly (it returns
     the worker's actual status) instead of guessing a fixed duration with
     `time.sleep`. Do not introduce a new sleep call anywhere, including
     inside a "poll loop" — any `time.sleep(...)` still fails the gate.
  3. Run the checker again to confirm.

Done when: `check_sleep.py` exits 0 (no `time.sleep(...)` calls remain in
test code).
Then output: <promise>VERIFIED</promise>

Do not edit `seed/check_sleep.py` — that is the gate, not the target.
Do not rename `time.sleep` to dodge the AST match (e.g. importing it under
an alias) — that would fake a green gate without fixing the flakiness.
Do not add new dependencies — the checker is pure standard library on
purpose.
