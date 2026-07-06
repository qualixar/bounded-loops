# OKR Measurability: fix okrs.json so every key result is measurable

Goal: make `python3 seed/check_okrs.py seed/okrs.json` report that every key
result carries a numeric target, a unit, and a deadline (exit 0).

Steps each turn:
  1. Run the checker:
     `python3 seed/check_okrs.py seed/okrs.json`
  2. For each key result it flags as vague, open `seed/okrs.json` and add
     whatever is missing:
     - a concrete numeric `target` (not null, not a string);
     - a non-empty `unit` describing what the target counts (e.g.
       "percent", "customers", "hours");
     - a `deadline` in `YYYY-MM-DD` form.
  3. Run the checker again to confirm.

Done when: `check_okrs.py` exits 0 (every key result is measurable).
Then output: <promise>MEASURABLE</promise>

Do not delete a key result to dodge the check — fix it so it is genuinely
measurable.
Do not edit `seed/check_okrs.py` — that is the gate, not the target.
Do not add new dependencies — the checker is pure standard library on purpose.
