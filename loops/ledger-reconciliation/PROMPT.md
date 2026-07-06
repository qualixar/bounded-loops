# Ledger Reconciliation: make the books balance

Goal: make `pytest -q` pass by reconciling `seed/ledger.csv`.

Steps each turn:
  1. Run: `pytest -q`
  2. If it fails, read the error, edit `seed/ledger.csv` to fix the cause.
  3. Run pytest again to confirm.

There are exactly two problems in the ledger:
  1. The ledger does not balance — the total of the `debit` column does not
     equal the total of the `credit` column. One row has a typo'd amount.
     Find it and correct it so debits equal credits.
  2. Exactly one transaction has an empty `category`. Categorize it using
     one of the allowed values: equity, expense, revenue, asset, liability.

Done when: pytest reports 0 failures.
Then output: <promise>GREEN</promise>

Do not edit `test_ledger.py`.
Do not delete any rows from `ledger.csv`.
Do not add new dependencies.
