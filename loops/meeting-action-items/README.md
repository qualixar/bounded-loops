# meeting-action-items

**Role:** business · **Rung:** L1 · **Gate:** `command` (keyless) · **Runner:** stub (keyless)

A bounded loop that drives an agent until **every action item in a
meeting-minutes document names an owner and a due date**. This is the
runnable form of the most common meeting-notes failure: an action item
that gets written down but has nobody's name on it and no deadline, so it
silently never happens.

## What it demonstrates

The seed `minutes.md` has three action items under "## Action Items". One
is unassigned and undated:

- **"Fix the Paddle pricing page copy flagged in the rejection."** — no
  `@owner`, no `YYYY-MM-DD` due date.

The gate `seed/check_actions.py` extracts the "## Action Items" section,
walks every bullet, and flags any that lack an `@name` owner tag or a
`YYYY-MM-DD` due date. The loop is DONE only when the checker exits 0.

## Run it (keyless, ~1s)

```bash
bl run loops/meeting-action-items --yes   # stub runner + real command gate
```

You'll see the unassigned action item fail the checker, the recorded fix
add an owner and a due date, then the gate pass.

## Why the checker is `forbid:`-protected

The whole point is that the agent conforms the *minutes* to a real
accountable standard. Letting it "fix" the failure by editing the checker
to stop requiring an owner/date would fake a green gate — exactly the
"agent talks its way past the verifier" failure bounded-loops exists to
prevent. The engine refuses any write to `seed/check_actions.py`.

## Make it real

Swap the stub runner for a real agent and point this at your actual
meeting-notes tool export (Otter, Fireflies, Notion, a raw Zoom
transcript-to-markdown pipeline). Keep the gate as the bottleneck: minutes
are never "done" until every action item has a name and a date attached.
