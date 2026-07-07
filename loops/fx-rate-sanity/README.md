# fx-rate-sanity

**Role:** finance · **Rung:** L2 · **Gate:** `command` (keyless) · **Runner:** stub (keyless)

A bounded loop that drives an agent until a table of FX rates is
**internally consistent**: every rate is positive, the base currency's
rate against itself is exactly 1, and every quoted inverse pair
multiplies to ~1 (the no-arbitrage identity).

## What it demonstrates

The seed `rates.json` ships a USD-base table including `USD/EUR: 0.92`
and `EUR/USD: 1.0870`. Their product is `1.0000...` off by more than
tolerance — a classic stale-quote drift where one side of a pair was
updated and the other wasn't. The gate `seed/check_fx.py` flags this as a
no-arbitrage violation.

## Run it (keyless, ~1s)

```bash
bl run loops/fx-rate-sanity --yes
```

## Why the checker is `forbid:`-protected

The point is that the agent conforms the *rate table* to internal
consistency. Letting it "fix" the failure by loosening the tolerance or
removing the inverse-pair check would fake a green gate. The engine
refuses any write to `seed/check_fx.py`.

## Make it real

Copy this loop into the repo that owns your rate-table export or pricing test
fixtures. Replace `seed/rates.json` with your feed shape and extend
`seed/check_fx.py` with the tolerance and currency-pair rules your treasury or
pricing team approves. In production, use `bounds.production.yaml` so a passing
gate routes to a human or downstream release approval before the rates are used.

## Which Anthropic pattern

`evaluator-optimizer` — the gate (`check_fx.py`) is the evaluator; the
agent-turn is the optimizer.
Reference: [Anthropic — Building effective agents](https://www.anthropic.com/research/building-effective-agents)
