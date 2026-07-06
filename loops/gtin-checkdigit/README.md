# gtin-checkdigit

**Role:** retail · **Rung:** L1 · **Gate:** `command` (keyless) · **Runner:** stub (keyless)

A bounded loop that drives an agent until **every product's GTIN-13 has a
valid mod-10 check digit**. This is the runnable form of a common retail
data-quality failure: a corrupted or mistyped barcode digit that silently
breaks POS scans, marketplace feed ingestion, and EDI reconciliation.

## What it demonstrates

The seed `products.json` lists two SKUs. One is broken:

- **SKU-200** — GTIN `4006381333935`; the correct GS1 mod-10 check digit
  for that prefix is `1`, not `5`.

The gate `seed/check_gtin.py` recomputes the check digit from the first 12
digits (alternating weights 1,3,1,3,...) and flags any GTIN whose 13th digit
doesn't match. The loop is DONE only when the checker exits 0.

## Run it (keyless, ~1s)

```bash
bl run loops/gtin-checkdigit --yes   # stub runner + real command gate
```

You'll see the corrupted GTIN fail the checker, the recorded fix correct the
single check digit, then the gate pass.

## Why the checker is `forbid:`-protected

The whole point is that the agent conforms the *GTIN* to the mod-10 formula.
Letting it edit the checker to skip validation would fake a green gate —
exactly the "agent talks its way past the verifier" failure bounded-loops
exists to prevent. The engine refuses any write to `seed/check_gtin.py`.

## Make it real

Swap the stub runner for a real agent and point `products.json` at your live
product catalog export. Keep the gate as the bottleneck: a feed is never
"done" until every GTIN is independently verified to carry a correct check
digit.
