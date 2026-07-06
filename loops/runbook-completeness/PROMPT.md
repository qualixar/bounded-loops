# Runbook Completeness: fix runbook.md so every required section is present

Goal: make `python3 seed/check_runbook.py seed/runbook.md` report that every
required section is present (exit 0).

Steps each turn:
  1. Run the checker:
     `python3 seed/check_runbook.py seed/runbook.md`
  2. For each section it flags as missing, add a matching markdown heading
     (any `#` level) to `seed/runbook.md` with real, useful operational
     content for that section — not a placeholder.
  3. Run the checker again to confirm.

Required sections: Summary, Severity, Detection, Diagnosis, Mitigation,
Rollback, Escalation.

Done when: `check_runbook.py` exits 0 (every required section present).
Then output: <promise>COMPLETE</promise>

Do not edit `seed/check_runbook.py` — that is the gate, not the target.
Do not add a heading with no real content just to satisfy the checker; a
runbook section must actually help an on-call engineer during an incident.
Do not add new dependencies — the checker is pure standard library on
purpose.
