# inventory-nonnegative

**Role:** retail · **Rung:** L2 · **Gate:** `command` (keyless) · **Runner:** stub (keyless)

A bounded loop that drives an agent until **a sequence of inventory
movements never oversells a SKU below zero**. This is the runnable form of a
common retail/WMS failure: a movement batch that overstates a decrement and
drives a SKU's tracked balance negative.

## What it demonstrates

The seed `movements.json` opens with `SKU-100: 10` and `SKU-200: 5`, then
replays four movements in order. One is broken:

- **SKU-200, movement index 1** — delta `-8` against a running balance of
  `5`, driving it to `-3`.

The gate `seed/check_inventory.py` replays every movement in order against
the opening balances and flags any SKU whose running balance goes negative
at any point. The loop is DONE only when the checker exits 0.

## Run it (keyless, ~1s)

```bash
bl run loops/inventory-nonnegative --yes   # stub runner + real command gate
```

You'll see the oversold movements fail the checker, the recorded fix reduce
SKU-200's offending delta so the balance never dips below zero, then the
gate pass.

## Why the checker is `forbid:`-protected

The whole point is that the agent conforms the *movement* to reality.
Letting it edit the checker to skip the negative-balance check would fake a
green gate — exactly the "agent talks its way past the verifier" failure
bounded-loops exists to prevent. The engine refuses any write to
`seed/check_inventory.py`.

## Make it real

Swap the stub runner for a real agent and point `movements.json` at (or have
the checker query) your live WMS ledger. Keep the gate as the bottleneck: a
movement batch is never "done" until every SKU is independently verified to
stay non-negative through the full replay.
