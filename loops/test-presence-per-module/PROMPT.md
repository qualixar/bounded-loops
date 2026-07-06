# Test Presence Per Module: give every source module a test file

Goal: make `python3 seed/check_test_presence.py seed/src seed/tests` report
that every module in `seed/src/` has a matching `seed/tests/test_<mod>.py`
file (exit 0).

Steps each turn:
  1. Run the checker:
     `python3 seed/check_test_presence.py seed/src seed/tests`
  2. For each module it flags as missing a test file, read the module in
     `seed/src/` and write a new `seed/tests/test_<mod>.py` containing at
     least one real test that imports the module and asserts on its actual
     behavior — not an empty file and not a test that imports nothing and
     asserts nothing.
  3. Run the checker again to confirm.

Done when: `check_test_presence.py` exits 0 (every `src/<mod>.py` has a
matching `tests/test_<mod>.py`).
Then output: <promise>VERIFIED</promise>

Do not edit `seed/check_test_presence.py` — that is the gate, not the
target.
Do not create an empty or no-op test file (e.g. one with no import and no
assert) just to satisfy the file-presence check — the new test must
actually exercise the module.
Do not add new dependencies — the checker is pure standard library on
purpose.
