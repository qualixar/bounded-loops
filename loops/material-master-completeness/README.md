# material-master-completeness

**Role:** enterprise · erp · **Rung:** L2 · **Gate:** `command` (keyless) · **Runner:** stub (keyless)

A bounded loop that drives an agent until a **SAP material master record**
carries every field its material type requires, mirroring SAP's own
material-type-driven field selection (transaction `OMS9`, field references
consulted during `MM01`/`MM02`): a finished product (`FERT`) needs a base
unit of measure, a description, and a sales division; a raw material
(`ROH`) needs only a base unit of measure and a description, since it is
procured and consumed rather than sold.

## What it demonstrates

The seed `material.json` ships a `FERT` (finished product) record with
`base_uom` and `description` populated but `division` left empty — a
record that would fail sales-area determination the moment someone tries
to create a sales order against it.

The gate `seed/check_material.py` looks up the required-field set for the
record's `material_type` and flags any missing or empty field. The loop is
DONE only when the checker exits 0.

## Run it (keyless, ~1s)

```bash
bl run loops/material-master-completeness --yes   # stub runner + real command gate
```

You'll see the ungated record fail the checker (empty division), the
recorded fix fill it in, then the gate pass.

## Why the checker is `forbid:`-protected

The whole point is that the agent conforms the *record* to its material
type's requirements. Letting it "fix" the failure by switching
`material_type` to `ROH` (which doesn't require division) instead of
filling in the field, or by editing the checker, would fake a green gate
— exactly the "agent talks its way past the verifier" failure
bounded-loops exists to prevent. The engine refuses any write to
`seed/check_material.py`.

## Make it real

Swap the stub runner for a real agent and point the gate at SAP's own
field-selection logic (a BAPI_MATERIAL_GET_DETAIL/BAPI_MATERIAL_SAVEDATA
round-trip, or the `OMS9` field-reference table via RFC). Keep the gate as
the bottleneck: a material master is never "done" until every field its
type requires is genuinely populated.
