# runbook-completeness

**Role:** operations · **Rung:** L1 · **Gate:** `command` (keyless) · **Runner:** stub (keyless)

A bounded loop that drives an agent until **an operations runbook contains
every section an on-call engineer needs during an incident**. This is the
runnable form of a common on-call failure: a runbook that reads well but
stops short of the two sections that matter most once something is actually
on fire — how to undo a bad mitigation, and who to page next.

## What it demonstrates

The seed `runbook.md` covers Summary, Severity, Detection, and Diagnosis,
but stops there. It is missing:

- **Rollback** — how to undo a mitigation that didn't work or made things
  worse.
- **Escalation** — who to page next, and when, if the on-call engineer
  can't resolve it alone.

The gate `seed/check_runbook.py` scans the document's markdown headings and
flags any of the seven required sections (Summary, Severity, Detection,
Diagnosis, Mitigation, Rollback, Escalation) that is missing. The loop is
DONE only when the checker exits 0.

## Run it (keyless, ~1s)

```bash
bl run loops/runbook-completeness --yes   # stub runner + real command gate
```

You'll see the incomplete runbook fail the checker, the recorded fix add
Mitigation, Rollback, and Escalation sections with real operational content,
then the gate pass.

## Why the checker is `forbid:`-protected

The whole point is that the agent conforms the *runbook* to the standard,
not the standard to the runbook. Letting it "fix" the failure by editing
the checker to drop a required section would fake a green gate — exactly
the "agent talks its way past the verifier" failure bounded-loops exists to
prevent. The engine refuses any write to `seed/check_runbook.py`.

## Make it real

Point the checker at your team's real runbook template and required
section list, and run it in CI on every runbook PR so an incomplete runbook
never merges. Swap the stub runner for a real agent to auto-draft the
missing sections from your incident history.
