# gdpr-dpa-terms

**Role:** legal · **Rung:** L2 · **Gate:** `command` (keyless) · **Runner:** stub (keyless)

A bounded loop that drives an agent until **a Data Processing Agreement
covers every mandatory term required by GDPR Article 28(3)**. This is the
runnable form of a common compliance gap: a DPA assembled from a partial
template that covers the obvious controller-processor terms but omits
sub-processor flow-down and audit rights.

## What it demonstrates

The seed `dpa.md` covers seven of the nine Art.28(3) categories but is
missing two:

- **Sub-Processor** — no term addressing engagement of, or flow-down of
  obligations to, sub-processors.
- **Audit** — no term granting the controller audit or inspection rights
  over the processor's compliance.

The gate `seed/check_dpa.py` scans the document for all nine mandatory
term categories (subject matter, duration, nature and purpose, type of
personal data, obligations of the controller, sub-processor,
confidentiality, security measures, audit) by case-insensitive keyword
match. The loop is DONE only when all nine are present.

## Run it (keyless, ~1s)

```bash
bl run loops/gdpr-dpa-terms --yes   # stub runner + real command gate
```

You'll see the incomplete DPA fail the checker, the recorded fix add the
two missing terms, then the gate pass.

## Why the checker is `forbid`-protected

The whole point is that the agent conforms the *document* to the legal
requirement, not the requirement to the document. Letting it "fix" the
failure by editing the checker to drop a mandatory term would fake a
green gate — exactly the "agent talks its way past the verifier" failure
bounded-loops exists to prevent. The engine refuses any write to
`seed/check_dpa.py`.

## Make it real

Swap the stub runner for a real agent and extend `MANDATORY_TERMS` in the
checker to match your organization's full Art.28(3)/Art.28(4) checklist,
or point it at a clause-classification model. Keep the gate as the
bottleneck: a DPA is never "done" until every mandatory term is
independently confirmed present.
