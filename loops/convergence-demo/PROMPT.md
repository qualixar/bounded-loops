# Convergence Demo: normalize a score correctly

Goal: make every test in `seed/test_score.py` pass by fixing
`seed/score.py`.

Steps each turn:
  1. Run `pytest -q` and read the specific failure.
  2. Improve `seed/score.py`; do not edit the tests.
  3. Run the gate again and continue until every boundary case passes.

Done when: pytest reports zero failures.

Do not edit `seed/test_score.py`.
Do not weaken or redirect pytest collection.
Do not add dependencies.
