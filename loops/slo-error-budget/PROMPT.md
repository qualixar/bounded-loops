# SLO Error Budget: fix slo.json so the reported budget is correct

Goal: make the test in `seed/test_error_budget.py` pass.

Steps each turn:
  1. Run: `pytest -q`
  2. If it fails, read the assertion error — it shows the correct value
     computed as `allowed - downtime_minutes` where
     `allowed = window_minutes * (1 - slo_target_pct / 100)` — and correct
     `budget_remaining_minutes` in `seed/slo.json` to that value.
  3. Run pytest again to confirm.

Done when: pytest reports 0 failures.
Then output: <promise>GREEN</promise>

Do not edit the test file.
Do not change `slo_target_pct`, `window_minutes`, or `downtime_minutes` to
make the existing `budget_remaining_minutes` value pass — only
`budget_remaining_minutes` is wrong; fix that field to match what the other
three actually allow.
Do not add new dependencies.
