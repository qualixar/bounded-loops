# privacy-policy-completeness

**Role:** legal · **Rung:** L2 · **Gate:** `command` (keyless) · **Runner:** stub (keyless)

A bounded loop that drives an agent until **a privacy policy covers every
section a working policy needs**. This is the runnable form of a common
publishing defect: a policy that explains what data is collected and how
it's shared, but never says how long it's kept or what rights an
individual has over it.

## What it demonstrates

The seed `privacy.md` has four sections but is missing two:

- **Data Retention** — no statement of how long collected data is kept.
- **Your Rights** — no description of access, correction, or deletion
  rights available to individuals.

The gate `seed/check_privacy.py` scans the document for six required
sections (Data We Collect, How We Use Your Data, Data Sharing, Data
Retention, Your Rights, Contact) by case-insensitive keyword/heading
match. The loop is DONE only when all six are present.

## Run it (keyless, ~1s)

```bash
bl run loops/privacy-policy-completeness --yes   # stub runner + real command gate
```

You'll see the incomplete policy fail the checker, the recorded fix add
the two missing sections, then the gate pass.

## Why the checker is `forbid`-protected

The whole point is that the agent conforms the *document* to the
requirement, not the requirement to the document. Letting it "fix" the
failure by editing the checker to drop a required section would fake a
green gate — exactly the "agent talks its way past the verifier" failure
bounded-loops exists to prevent. The engine refuses any write to
`seed/check_privacy.py`.

## Make it real

Swap the stub runner for a real agent and extend `REQUIRED_SECTIONS` in
the checker to match your organization's privacy notice checklist (e.g.
CCPA/CPRA disclosures, cookie categories, cross-border transfer
mechanisms), or point it at a policy-classification model. Keep the gate
as the bottleneck: a privacy policy is never "done" until every required
section is independently confirmed present.
