# cds-view-annotations

**Role:** enterprise · erp · **Rung:** L2 · **Gate:** `command` (keyless) · **Runner:** stub (keyless)

A bounded loop that drives an agent until a **SAP Core Data Services (CDS)
view** DDL source declares the annotations SAP best practice treats as
mandatory before release: `@AccessControl.authorizationCheck` (how the view
enforces authorization — a gap here is a security exposure once the view
is consumed by Fiori/OData), `@EndUserText.label` (the business-friendly
label shown in the data dictionary and Fiori Elements UI), and
`@Metadata.allowExtensions` (the S/4HANA customer-extensibility contract).

## What it demonstrates

The seed `zcds_view.txt` ships a real invoice-summary CDS view with
`@Metadata.allowExtensions` present but `@AccessControl.authorizationCheck`
set only implicitly (missing the explicit annotation) and `@EndUserText.label`
entirely absent — a view that would fail a customer ABAP release-quality
guideline.

The gate `seed/check_cds.py` does a simple substring check for all three
required annotations. The loop is DONE only when the checker exits 0.

## Run it (keyless, ~1s)

```bash
bl run loops/cds-view-annotations --yes   # stub runner + real command gate
```

You'll see the ungated view fail the checker (two annotations missing),
the recorded fix add both, then the gate pass.

## Why the checker is `forbid:`-protected

The whole point is that the agent conforms the *view* to the required
annotation contract. Letting it "fix" the failure by editing the checker
to stop requiring the annotations would fake a green gate — exactly the
"agent talks its way past the verifier" failure bounded-loops exists to
prevent. The engine refuses any write to `seed/check_cds.py`.

## Make it real

Swap the stub runner for a real agent and point the gate at a real ABAP
Test Cockpit (ATC) check, ADT's own CDS linter, or a custom `abaplint`
rule that greps release-candidate DDL sources. Keep the gate as the
bottleneck: a CDS view is never "done" until its release-quality
annotations are complete.
