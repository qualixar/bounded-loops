# Journal Balance: fix journal.json so every entry balances

Goal: make `python3 seed/check_balance.py seed/journal.json` report that
every entry's debits equal its credits (exit 0).

Steps each turn:
  1. Run the checker:
     `python3 seed/check_balance.py seed/journal.json`
  2. For each entry it flags as unbalanced, open `seed/journal.json` and
     find the line whose debit or credit amount is wrong, then correct it
     so total debit equals total credit for that entry.
  3. Run the checker again to confirm.

Done when: `check_balance.py` exits 0 (every entry balances).
Then output: <promise>BALANCED</promise>

Do not edit `seed/check_balance.py` — that is the gate, not the target.
Do not add new dependencies — the checker is pure standard library on purpose.
