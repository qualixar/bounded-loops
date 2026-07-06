# Test Naming Contract: rename every misnamed test function

Goal: make `python3 seed/check_test_names.py seed/sample_tests.py` report
that every test-like function is named `test_*` (exit 0).

Steps each turn:
  1. Run the checker:
     `python3 seed/check_test_names.py seed/sample_tests.py`
  2. For each function it flags as misnamed, open `seed/sample_tests.py` and
     rename that function so it starts with `test_` (e.g. `check_addition` ->
     `test_addition`). Do not change the function's behavior or assertions —
     only the name — and update nothing else.
  3. Run the checker again to confirm.

Done when: `check_test_names.py` exits 0 (every test-like function starts
with `test_`).
Then output: <promise>VERIFIED</promise>

Do not edit `seed/check_test_names.py` — that is the gate, not the target.
Do not delete the misnamed function to "fix" the failure — rename it in
place so it is actually collected by a test runner.
Do not add new dependencies — the checker is pure standard library on
purpose.
