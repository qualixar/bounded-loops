# AutoGen (Microsoft Agent Framework) example: fix seed/app.py so seed/test_app.py passes

Goal: make the test in `seed/test_app.py` pass.

Steps each turn:
  1. Run: `pytest -q`
  2. If it fails, read the error, edit `seed/app.py` to fix the cause.
  3. Run pytest again to confirm.

Done when: pytest reports 0 failures.

Do not edit the test file.
Do not add new dependencies.
