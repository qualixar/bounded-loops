# ledger-reconciliation

**Pattern:** evaluator-optimizer · **Role:** finance, accounting · **Rung:** L2 · **Gate:** pytest

A finance-industry bounded loop: drive an agent until a double-entry ledger
reconciles. This proves bounded-loops spans industries, not just software —
the gate is still just `pytest -q`, but the domain is accounting, not code.
Keyless: the gate uses only the Python standard library (`csv`, `pathlib`).

## What happens

A ledger ships with two real problems: the `debit` and `credit` columns
don't sum to the same total (one row has a typo'd amount), and one
transaction has no `category`. The loop runs an agent against `PROMPT.md`,
checks `pytest -q` after each lap, and halts as soon as the gate is green.
The agent cannot exit early by claiming success — the arithmetic and the
category check are ground truth, not the agent's opinion.

## Prove the gate genuinely fails on the unfixed seed

Real captured output, run from the repo root against the broken seed as
shipped in `seed/`:

```
$ .venv/bin/python -m pytest -q loops/ledger-reconciliation/seed/
FF                                                                       [100%]
=================================== FAILURES ===================================
______________ TestLedgerReconciliation.test_debits_equal_credits ______________

self = <test_ledger.TestLedgerReconciliation object at 0x1053b0550>

    def test_debits_equal_credits(self):
        rows = _read_rows()
        total_debit = sum(float(r["debit"]) for r in rows if r["debit"].strip())
        total_credit = sum(float(r["credit"]) for r in rows if r["credit"].strip())
>       assert total_debit == total_credit, (
            f"ledger does not balance: total_debit={total_debit} "
            f"total_credit={total_credit}"
        )
E       AssertionError: ledger does not balance: total_debit=12780.0 total_credit=13500.0
E       assert 12780.0 == 13500.0

loops/ledger-reconciliation/seed/test_ledger.py:20: AssertionError
_________ TestLedgerReconciliation.test_every_transaction_categorized __________

self = <test_ledger.TestLedgerReconciliation object at 0x10ae71010>

    def test_every_transaction_categorized(self):
        rows = _read_rows()
        for r in rows:
            category = r["category"].strip()
>           assert category, f"row id={r['id']} has an empty category"
E           AssertionError: row id=5 has an empty category

loops/ledger-reconciliation/seed/test_ledger.py:29: AssertionError
=========================== short test summary info ============================
FAILED loops/ledger-reconciliation/seed/test_ledger.py::TestLedgerReconciliation::test_debits_equal_credits
FAILED loops/ledger-reconciliation/seed/test_ledger.py::TestLedgerReconciliation::test_every_transaction_categorized
2 failed in 0.02s
```

Both failure modes are real: the ledger is out of balance by exactly
720.00 (row 5's contractor payment was typo'd as `1080.00` instead of
`1800.00`), and row 5 also has an empty `category`. Correcting both —
restoring the amount to `1800.00` and setting `category` to `expense` —
makes both assertions pass, confirmed against the same test file.

## Run it with the engine

```bash
# from repo root
.venv/bin/bl lint loops/ledger-reconciliation
.venv/bin/bl run loops/ledger-reconciliation --yes
```

Real captured output:
```
$ .venv/bin/bl lint loops/ledger-reconciliation
[PASS] .../bounded-loops/loops/ledger-reconciliation

$ .venv/bin/bl run loops/ledger-reconciliation --yes
[bounded-loops] About to run loop 'ledger-reconciliation':
  runner : stub
  gate   : pytest -q
✓ [DONE] gate-passed (laps: 1)  ledger: .../loops/ledger-reconciliation/.ledger.jsonl
```

The engine never touches `loops/ledger-reconciliation/seed/` in place — it
copies `seed/` into an isolated scratch dir and runs the stub agent + gate
there (bound: sandbox). The real `seed/ledger.csv` on disk stays in its
broken, unbalanced state after `bl run` finishes; only the scratch copy
gets fixed.

## Lift it into your own repo

1. Copy this folder.
2. Replace `seed/ledger.csv` and `seed/test_ledger.py` with your own ledger
   (or any records-must-reconcile dataset) and its invariants.
3. Edit `PROMPT.md` to describe your goal.
4. Point `runner.default` at a real agent CLI (e.g. `claude -p`) instead of
   the stub cassette, then `bl run` for the full engine.

## Which Anthropic pattern

`evaluator-optimizer` — the gate (pytest, checking arithmetic + categorization
invariants) is the evaluator; the agent-turn is the optimizer. The loop runs
until the evaluator says green.
Reference: [Anthropic — Building effective agents](https://www.anthropic.com/research/building-effective-agents)
