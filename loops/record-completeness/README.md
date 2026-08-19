# record-completeness

**Role:** quality · engineering · **Rung:** L2 · **Gate:** `command` (keyless) · **Runner:** shell (keyless)

A bounded loop that drives an agent until **every record in a JSON dataset carries a checksum**.
Its purpose in this catalogue is different from the others: it is the loop where you can watch a
declared bound actually being spent.

## What it demonstrates

Most loops here converge in one or two attempts, so a `max_iterations` of 5 is never approached and
the number tells you nothing. Here the shipped worker repairs **one record per attempt**, so the
attempts consumed are a function of the workload. `seed/records.json` holds eight records with no
checksum, so the run takes eight laps — and you can see the bound being drawn down against real
work instead of a limit that is never reached.

The gate, `seed/check_records.py`, is dependency-free standard library: no network, no external
tool, no API key. It reports the outstanding violation **count**, not a bare verdict, so progress is
visible per attempt and a predicted convergence length can be checked against the observed one.

Both the gate and the worker are in `forbid`. An agent that can edit its own acceptance criterion is
not gated at all, and an agent that can rewrite its own body can satisfy the gate without doing the
task.

## The unfixed seed fails

```bash
$ python3 seed/check_records.py seed/records.json
check_records: 8 of 8 records missing a checksum
$ echo $?
1
```

## Run it (keyless, ~1s)

```bash
bl run loops/record-completeness --yes
```

Expected output — the gate passes only after every record is complete:

```
✓ [DONE] gate-passed (laps: 8)  ledger: ./loops/record-completeness/.ledger.jsonl
Gate verified: the independent acceptance gate passed after 8 laps.
```

Eight laps for eight records is the point. Lower `max_iterations` in `bounds.yaml` to 4 and the same
run halts at the ceiling with a handoff instead — a bound that holds, on a task that genuinely
needed more.

## Lift it into your own repo

Replace `seed/records.json` with your own dataset and `seed/check_records.py` with the completeness
rule you actually need — a required column in an export, a mandatory field in an event payload, a
signature on every row. Keep two properties and the loop keeps working:

- The gate exits non-zero while any record is outstanding, and reports **how many**. The count is
  what makes convergence observable rather than a guess.
- The gate stays in `forbid`, along with anything the agent could edit to pass without doing the
  work.

This loop is **L2**, so a production `bounds.yaml` should keep `require_approval` on, or ship a
`bounds.production.yaml` that does. Replace the shipped `shell` runner with your real agent command;
the gate does not change.
