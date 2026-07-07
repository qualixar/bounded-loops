# journal-entries-balance

**Role:** finance · **Rung:** L2 · **Gate:** `command` (keyless) · **Runner:** stub (keyless)

A bounded loop that drives an agent until **every entry in a double-entry
journal balances** — the fundamental invariant that sum(debit) equals
sum(credit) for each posting.

## What it demonstrates

The seed `journal.json` ships three entries. Two balance; one — `JE-1002`,
a rent payment — does not: debits total 200.00 but credits total 250.00.
The gate `seed/check_balance.py` sums each entry's debit and credit lines
and flags any entry where they disagree beyond a 1e-6 tolerance.

## Run it (keyless, ~1s)

```bash
bl run loops/journal-entries-balance --yes
```

## Why the checker is `forbid:`-protected

The point is that the agent conforms the *journal* to the balancing
invariant. Letting it "fix" the failure by loosening the checker's
tolerance or removing the check would fake a green gate. The engine
refuses any write to `seed/check_balance.py`.

## Make it real

Copy this loop into the repo or data pipeline that prepares journal entries
before posting. Replace `seed/journal.json` with your journal-entry export shape
and extend `seed/check_balance.py` with your account, currency, rounding, and
entity-specific posting rules. In production, use `bounds.production.yaml` so a
passing gate means ready for accounting review or posting approval, not automatic
posting to the ledger.

## Which Anthropic pattern

`evaluator-optimizer` — the gate (`check_balance.py`) is the evaluator; the
agent-turn is the optimizer.
Reference: [Anthropic — Building effective agents](https://www.anthropic.com/research/building-effective-agents)
