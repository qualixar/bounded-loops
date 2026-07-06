# Inventory Non-Negative: fix movements.json so no SKU oversells below zero

Goal: make `python3 seed/check_inventory.py seed/movements.json` report that
every SKU's running balance stays non-negative through the whole movement
sequence (exit 0).

Steps each turn:
  1. Run the checker:
     `python3 seed/check_inventory.py seed/movements.json`
  2. For each SKU it flags as going negative, find the offending movement in
     `seed/movements.json` (the one whose delta pushes the running balance
     below zero) and reduce that delta's magnitude so the balance stays
     `>= 0` at every point in the replay.
  3. Run the checker again to confirm.

Done when: `check_inventory.py` exits 0 (every SKU's balance stayed
non-negative throughout the replay).
Then output: <promise>VERIFIED</promise>

Do not edit `seed/check_inventory.py` — that is the gate, not the target.
Do not reorder movements or edit opening balances to hide the oversell —
correct the offending movement's magnitude.
Do not add new dependencies — the checker is pure standard library on
purpose.
