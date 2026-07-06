# slo-error-budget

**Role:** operations · engineering · **Rung:** L1 · **Gate:** `pytest` (keyless) · **Runner:** stub (keyless)

A bounded loop that drives an agent until **an SLO's reported error-budget
remainder matches what the target, window, and downtime actually allow**.
This is the runnable form of a quiet but dangerous ops bug: a dashboard
number that looks plausible but doesn't reconcile with the math behind it,
so an on-call engineer trusts a budget figure that's simply wrong.

## What it demonstrates

The seed `slo.json` declares a 99.9% target over a 30-day (43,200-minute)
window with 30 minutes of downtime already consumed. The allowed error
budget for that window is `43200 * (1 - 99.9/100) = 43.2` minutes, so the
correct `budget_remaining_minutes` is `43.2 - 30 = 13.2`. The seed instead
reports `20` — a stale or hand-typed number that doesn't reconcile.

`seed/test_error_budget.py` is the ground truth: it recomputes the correct
value from `window_minutes`, `slo_target_pct`, and `downtime_minutes`, and
asserts `budget_remaining_minutes` matches. The loop is DONE only when
pytest is green.

## Run it (keyless, ~1s)

```bash
bl run loops/slo-error-budget --yes   # stub runner + real pytest gate
```

You'll see the wrong budget fail the test, the recorded fix correct
`budget_remaining_minutes` to `13.2`, then pytest go green.

## Why the test is `forbid:`-protected

The whole point is that the agent conforms the *data* to the math, not the
math to the data. Letting it "fix" the failure by editing the test to
accept `20` would fake a green gate — exactly the "agent talks its way past
the verifier" failure bounded-loops exists to prevent. The engine refuses
any write to `seed/test_*.py`.

## Make it real

Point this at your real SLO tracker's export and run it as a CI check on
every SLO config change, so a miscalculated budget never reaches an
on-call dashboard.
