# invoice-3way-match

**Role:** finance · **Rung:** L2 · **Gate:** `pytest` (keyless) · **Runner:** stub (keyless)

A bounded loop that drives an agent until an invoice passes the classic
accounts-payable **three-way match**: every line's unit price agrees with the
purchase order, and every line's quantity agrees with the goods receipt.

## What it demonstrates

The seed ships `purchase_order.json` (100/50/20 units of WIDGET-A/B/C) and a
matching `goods_receipt.json`. `invoice.json` bills 65 units of WIDGET-B
instead of the 50 actually received — a classic over-billing defect a
three-way match exists to catch. The gate (`seed/test_three_way_match.py`)
fails until the invoice is corrected.

## Run it (keyless, ~1s)

```bash
bl run loops/invoice-3way-match --yes
```

## Why the test and source docs are `forbid:`-protected

The point is that the agent conforms the *invoice* to the PO/goods-receipt
ground truth. Letting it "fix" the failure by editing the PO, the goods
receipt, or the test itself would fake a green gate — the engine refuses any
write to `seed/test_*.py`.

## Make it real

Copy this loop into an AP, procurement, or ERP integration repo. Replace the
three JSON seed files with exports from your invoice, purchase-order, and goods
receipt systems, then extend `seed/test_three_way_match.py` with your tolerance,
tax, freight, and unit-of-measure rules. In production, use
`bounds.production.yaml` so a passing three-way match means ready for AP review
or workflow approval, not automatic payment.

## Which Anthropic pattern

`evaluator-optimizer` — the gate (pytest) is the evaluator; the agent-turn is
the optimizer.
Reference: [Anthropic — Building effective agents](https://www.anthropic.com/research/building-effective-agents)
