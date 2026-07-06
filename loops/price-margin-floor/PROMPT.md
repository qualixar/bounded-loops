# Price Margin Floor: fix catalog.json so every SKU clears the margin policy

Goal: make `python3 seed/check_margin.py seed/catalog.json seed/policy.json`
report that every SKU's price is at or above its minimum-margin floor
(exit 0).

Steps each turn:
  1. Run the checker:
     `python3 seed/check_margin.py seed/catalog.json seed/policy.json`
  2. For each SKU it flags as below the margin floor, open
     `seed/policy.json` (read-only ground truth) to see `min_margin`, and
     raise that SKU's `price` in `seed/catalog.json` to at least
     `cost * (1 + min_margin)`.
  3. Run the checker again to confirm.

Done when: `check_margin.py` exits 0 (every SKU meets or exceeds the margin
floor).
Then output: <promise>VERIFIED</promise>

Do not edit `seed/policy.json` — the policy is the ground truth, not
something to loosen to make an underpriced SKU "pass".
Do not edit `seed/check_margin.py` — that is the gate, not the target.
Do not add new dependencies — the checker is pure standard library on
purpose.
