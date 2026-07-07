# expense-policy-compliance

**Role:** finance · **Rung:** L2 · **Gate:** `command` (keyless) · **Runner:** stub (keyless)

A bounded loop that drives an agent until **every line of an expense
report complies with company policy**: an allowed category, and within
that category's per-line cap.

## What it demonstrates

The seed `expenses.json` ships five lines against `policy.json`'s caps
(meals $75, travel $1200, lodging $300, supplies $150, software $500).
Two lines violate policy: a $110.00 "Dinner with prospect" meal exceeds
the $75 meals cap, and a $60.00 "Client concert tickets" line is
categorized as `entertainment`, which is not in `allowed_categories` at
all. The gate `seed/check_expenses.py` flags both.

## Run it (keyless, ~1s)

```bash
bl run loops/expense-policy-compliance --yes
```

## Why the checker and policy are `forbid:`-protected

The point is that the agent conforms the *expense report* to the policy.
Letting it "fix" the failure by raising the meals cap, adding
`entertainment` to `allowed_categories`, or editing the checker would fake
a green gate. The engine refuses any write to `seed/check_expenses.py` or
`seed/policy.json`.

## Make it real

Copy this loop into your finance or AP repo, replace `seed/expenses.json` with
the export shape your expense system produces, and replace `seed/policy.json`
with your approved policy caps and categories. Keep `seed/check_expenses.py` or
your equivalent policy checker protected with `forbid:`. In production, use the
included `bounds.production.yaml` so a passing gate means ready for AP review,
not automatic reimbursement.

## Which Anthropic pattern

`evaluator-optimizer` — the gate (`check_expenses.py`) is the evaluator;
the agent-turn is the optimizer.
Reference: [Anthropic — Building effective agents](https://www.anthropic.com/research/building-effective-agents)
