# Bug Fix: make the test pass

Goal: make the test in `seed/test_slugify.py` pass.

Steps each turn:
  1. Run: `pytest -q`
  2. If it fails, read the error, edit `seed/slugify.py` to fix the cause.
  3. Run pytest again to confirm.

Done when: pytest reports 0 failures.
Then output: <promise>GREEN</promise>

Do not edit the test file.
Do not add new dependencies.
