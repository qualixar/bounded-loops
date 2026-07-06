# Expense Policy Compliance: fix expenses.json so every line complies

Goal: make `python3 seed/check_expenses.py seed/expenses.json seed/policy.json`
report that every expense line is in an allowed category and within its cap
(exit 0).

Steps each turn:
  1. Run the checker:
     `python3 seed/check_expenses.py seed/expenses.json seed/policy.json`
  2. For each violation it flags, open `seed/policy.json` (read-only ground
     truth) and decide:
     - the line is over its category cap → reduce the amount to at or
       below the cap, or split/adjust it so it's compliant;
     - the line is in a disallowed category → recategorize it to the
       closest allowed category from `policy.json`'s `allowed_categories`.
  3. Run the checker again to confirm.

Done when: `check_expenses.py` exits 0 (every line complies).
Then output: <promise>COMPLIANT</promise>

Do not edit `seed/check_expenses.py` — that is the gate, not the target.
Do not edit `seed/policy.json` — the policy is ground truth, not something
to loosen to make a non-compliant expense "compliant".
Do not add new dependencies — the checker is pure standard library on purpose.
