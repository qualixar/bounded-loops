# price-margin-floor

**Role:** retail · **Rung:** L2 · **Gate:** `command` (keyless) · **Runner:** stub (keyless)

A bounded loop that drives an agent until **every SKU in a retail catalog is
priced at or above its minimum-margin policy floor**. This is the runnable
form of a common retail pricing failure: an underpriced SKU slipping through
that erodes margin below the business's own floor.

## What it demonstrates

The seed `catalog.json` lists three SKUs. One is broken:

- **SKU-200** — cost `20.00`, priced at `21.50` (7.5% margin) against a
  policy floor of 15% (`23.00` minimum).

The gate `seed/check_margin.py` reads the floor from `policy.json` and flags
any item where `price < cost * (1 + min_margin)`. The loop is DONE only when
the checker exits 0.

## Run it (keyless, ~1s)

```bash
bl run loops/price-margin-floor --yes   # stub runner + real command gate
```

You'll see the underpriced catalog fail the checker, the recorded fix raise
SKU-200's price above the floor, then the gate pass.

## Why the policy and checker are `forbid:`-protected

The whole point is that the agent conforms the *catalog* to the policy.
Letting it "fix" the failure by lowering `min_margin` in `policy.json`, or by
editing the checker, would fake a green gate — exactly the "agent talks its
way past the verifier" failure bounded-loops exists to prevent. The engine
refuses any write to `seed/check_margin.py` or `seed/policy.json`.

## Make it real

Swap the stub runner for a real agent and point `catalog.json` at (or have
the checker query) your live product-pricing feed. Keep the gate as the
bottleneck: a re-price is never "done" until every SKU is independently
verified to clear the margin floor.
