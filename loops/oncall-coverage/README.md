# oncall-coverage

**Role:** operations · **Rung:** L1 · **Gate:** `command` (keyless) · **Runner:** stub (keyless)

A bounded loop that drives an agent until **an on-call schedule covers
every hour of its rotation period exactly once**. This is the runnable form
of a common ops failure: a schedule that reads fine as three tidy shifts in
a spreadsheet but leaves a silent gap where an incident would page nobody.

## What it demonstrates

The seed `schedule.json` defines three shifts across a 24-hour period —
alice (0-8), bob (8-14), carol (16-24) — but hours 14 through 15 are
uncovered entirely: a 2-hour gap where no engineer is on call.

The gate `seed/check_coverage.py` counts, for every hour in
`[0, period_hours)`, how many shifts cover it, and flags any hour covered
zero times (a gap) or more than once (an overlap). The loop is DONE only
when the checker exits 0.

## Run it (keyless, ~1s)

```bash
bl run loops/oncall-coverage --yes   # stub runner + real command gate
```

You'll see the gapped schedule fail the checker, the recorded fix extend
bob's shift to close the 14-16 gap, then the gate pass.

## Why the checker is `forbid:`-protected

The whole point is that the agent conforms the *schedule* to full coverage,
not the coverage standard to the schedule. Letting it "fix" the failure by
editing the checker to ignore the gap would fake a green gate — exactly the
"agent talks its way past the verifier" failure bounded-loops exists to
prevent. The engine refuses any write to `seed/check_coverage.py`.

## Make it real

Point the checker at your real PagerDuty/Opsgenie schedule export and run
it on every rotation change before it goes live, so a gap never reaches
production. Swap the stub runner for a real agent to auto-propose the fix.
