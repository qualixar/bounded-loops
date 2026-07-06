# Alert Runbook Link: fix alerts.json so every alert links to a runbook

Goal: make `python3 seed/check_alerts.py seed/alerts.json` report that
every alert has a valid `runbook_url` (exit 0).

Steps each turn:
  1. Run the checker:
     `python3 seed/check_alerts.py seed/alerts.json`
  2. For each alert it flags, fill in a real, plausible `runbook_url`
     (starting with `http`) in `seed/alerts.json` — following the same
     `https://runbooks.internal/<alert-name>` convention already used by
     the other alerts in the file.
  3. Run the checker again to confirm.

Done when: `check_alerts.py` exits 0 (every alert has a non-empty
`http`-prefixed `runbook_url`).
Then output: <promise>LINKED</promise>

Do not edit `seed/check_alerts.py` — that is the gate, not the target.
Do not delete an alert to dodge the check; every alert must keep its
`alert` name and gain a real `runbook_url`.
Do not add new dependencies — the checker is pure standard library on
purpose.
