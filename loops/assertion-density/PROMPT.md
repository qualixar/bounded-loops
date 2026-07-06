# Assertion Density: give every test a real assertion

Goal: make `python3 seed/check_assertions.py seed/sample_tests.py` report
that every `test_*` function contains at least one `assert` (exit 0).

Steps each turn:
  1. Run the checker:
     `python3 seed/check_assertions.py seed/sample_tests.py`
  2. For each function it flags as assertion-free, open
     `seed/sample_tests.py` and add a meaningful `assert` that actually
     checks the result the test already computes (do not just add
     `assert True` — that would satisfy the checker without checking
     anything real).
  3. Run the checker again to confirm.

Done when: `check_assertions.py` exits 0 (every `test_*` function contains
at least one assert).
Then output: <promise>VERIFIED</promise>

Do not edit `seed/check_assertions.py` — that is the gate, not the target.
Do not add a tautological assert (`assert True`, `assert 1 == 1`) to game
the checker — the assertion must actually verify the behavior under test.
Do not add new dependencies — the checker is pure standard library on
purpose.
