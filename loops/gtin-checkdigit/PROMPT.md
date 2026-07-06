# GTIN Check Digit: fix products.json so every GTIN-13 is valid

Goal: make `python3 seed/check_gtin.py seed/products.json` report that every
product's GTIN-13 has a valid mod-10 check digit (exit 0).

Steps each turn:
  1. Run the checker:
     `python3 seed/check_gtin.py seed/products.json`
  2. For each SKU it flags, recompute the correct check digit yourself
     (weights 1,3,1,3,... across the first 12 digits, left to right; check
     digit = `(10 - (weighted_sum mod 10)) mod 10`) and correct the final
     digit of that GTIN in `seed/products.json`.
  3. Run the checker again to confirm.

Done when: `check_gtin.py` exits 0 (every GTIN-13's check digit is valid).
Then output: <promise>VERIFIED</promise>

Do not edit `seed/check_gtin.py` — that is the gate, not the target.
Do not change any digit other than the check digit (the 13th digit) unless
the identifying digits themselves are also wrong — prefer the minimal fix.
Do not add new dependencies — the checker is pure standard library on
purpose.
