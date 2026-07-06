# alert-runbook-link

**Role:** operations · **Rung:** L1 · **Gate:** `command` (keyless) · **Runner:** stub (keyless)

A bounded loop that drives an agent until **every alert definition links to
a non-empty runbook URL**. This is the runnable form of a common
operations failure: an alert that pages someone at 3am with no link to the
diagnosis and mitigation steps they need, because the `runbook_url` field
was left blank when the alert was created.

## What it demonstrates

The seed `alerts.json` defines four alerts; three have a real
`https://runbooks.internal/<name>` link and one —
`queue-depth-critical` — has an empty `runbook_url`.

The gate `seed/check_alerts.py` flags any alert whose `runbook_url` is
empty or doesn't start with `http`. The loop is DONE only when the checker
exits 0.

## Run it (keyless, ~1s)

```bash
bl run loops/alert-runbook-link --yes   # stub runner + real command gate
```

You'll see the alert with the missing link fail the checker, the recorded
fix fill in `https://runbooks.internal/queue-depth-critical` following the
same convention as the other alerts, then the gate pass.

## Why the checker is `forbid:`-protected

The whole point is that the agent conforms the *alert definitions* to the
requirement, not the requirement to the definitions. Letting it "fix" the
failure by editing the checker to allow an empty `runbook_url` would fake
a green gate — exactly the "agent talks its way past the verifier" failure
bounded-loops exists to prevent. The engine refuses any write to
`seed/check_alerts.py`.

## Make it real

Point the checker at your real alerting config export (Prometheus
Alertmanager, Datadog monitors, PagerDuty services) and run it in CI on
every alert-rule change, so an unlinked alert never reaches production.
