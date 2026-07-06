# On-Call Coverage: fix schedule.json so every hour is covered

Goal: make `python3 seed/check_coverage.py seed/schedule.json` report that
every hour of the rotation period is covered exactly once (exit 0).

Steps each turn:
  1. Run the checker:
     `python3 seed/check_coverage.py seed/schedule.json`
  2. For each gap it flags, extend an adjacent shift's `start`/`end` to
     cover the gap, or add a new shift entry for the uncovered hours, in
     `seed/schedule.json`.
  3. Run the checker again to confirm.

Done when: `check_coverage.py` exits 0 (every hour in `[0, period_hours)`
covered by exactly one shift).
Then output: <promise>COVERED</promise>

Do not edit `seed/check_coverage.py` — that is the gate, not the target.
Do not create an overlap while closing a gap; each hour must end up
covered by exactly one shift.
Do not add new dependencies — the checker is pure standard library on
purpose.
