# okr-measurable

**Role:** business · **Rung:** L1 · **Gate:** `command` (keyless) · **Runner:** stub (keyless)

A bounded loop that drives an agent until **every key result in an OKR
document is genuinely measurable**: a numeric target, a unit, and a
deadline. This is the runnable form of the most common OKR failure — a key
result that reads like an aspiration ("increase conversion rate") instead of
a number anyone can grade at quarter-end.

## What it demonstrates

The seed `okrs.json` has two objectives. One key result is vague:

- **"Increase course landing page conversion rate"** — `target: null`,
  `unit: ""`. Nothing to grade.

The gate `seed/check_okrs.py` walks every key result and flags any whose
`target` isn't a real number, whose `unit` is empty, or whose `deadline`
isn't `YYYY-MM-DD`. The loop is DONE only when the checker exits 0.

## Run it (keyless, ~1s)

```bash
bl run loops/okr-measurable --yes   # stub runner + real command gate
```

You'll see the vague key result fail the checker, the recorded fix add a
concrete target + unit, then the gate pass.

## Why the checker is `forbid:`-protected

The whole point is that the agent conforms the *OKR document* to a real,
measurable standard. Letting it "fix" the failure by editing the checker
to accept vague key results would fake a green gate — exactly the "agent
talks its way past the verifier" failure bounded-loops exists to prevent.
The engine refuses any write to `seed/check_okrs.py`.

## Make it real

Swap the stub runner for a real agent and point this at your actual OKR
tracker export (Lattice, Ally.io, a spreadsheet-to-JSON export, or your own
planning doc). Keep the gate as the bottleneck: an OKR is never "done"
being drafted until every key result is something you can actually grade.
