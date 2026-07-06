# idoc-xml-schema

**Role:** enterprise · erp · **Rung:** L2 · **Gate:** `command` (keyless) · **Runner:** stub (keyless)

A bounded loop that drives an agent until a simplified **SAP IDoc**
(Intermediate Document) XML file is structurally valid. IDocs are SAP's
standard format for exchanging business documents (orders, deliveries,
invoices) between systems; a structurally incomplete inbound IDoc is
rejected by the receiving function module into status 51 (application
error) instead of being posted.

## What it demonstrates

The seed `idoc.xml` ships an order IDoc with a complete control record
(`EDI_DC40`: MANDT, DOCNUM, MESTYP) and header (`E1EDK01`), but its single
item segment `E1EDP01` is missing the `MENGE` (quantity) field — an item
line with no quantity is meaningless and would fail SAP's own inbound
processing.

The gate `seed/check_idoc.py` uses `xml.etree.ElementTree` to require the
root `<IDOC>`, the control segment with its three mandatory fields, the
header segment, and at least one item segment with a non-empty quantity.
The loop is DONE only when the checker exits 0.

## Run it (keyless, ~1s)

```bash
bl run loops/idoc-xml-schema --yes   # stub runner + real command gate
```

You'll see the ungated document fail the checker (missing quantity), the
recorded fix add `<MENGE>`, then the gate pass.

## Why the checker is `forbid:`-protected

The whole point is that the agent conforms the *document* to the required
structure. Letting it "fix" the failure by editing the checker to stop
checking for `MENGE` would fake a green gate — exactly the "agent talks its
way past the verifier" failure bounded-loops exists to prevent. The engine
refuses any write to `seed/check_idoc.py`.

## Make it real

Swap the stub runner for a real agent and point the gate at a real IDoc
validation path — SAP's own segment editor (WE30/WE31), a schema derived
from the IDoc type's metadata, or a middleware (PI/PO, BTP Integration
Suite) validation step. Keep the gate as the bottleneck: an IDoc is never
"done" until it is structurally complete.
