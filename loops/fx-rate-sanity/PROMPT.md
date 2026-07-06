# FX Rate Sanity: fix rates.json so the pairs are internally consistent

Goal: make `python3 seed/check_fx.py seed/rates.json` report that every rate
is positive, the base-to-base rate is 1, and every inverse pair multiplies
to ~1 (exit 0).

Steps each turn:
  1. Run the checker:
     `python3 seed/check_fx.py seed/rates.json`
  2. If it flags a no-arbitrage violation between "A/B" and "B/A", pick the
     stale side and correct it in `seed/rates.json` so `A/B * B/A == 1`
     (within 1e-6). Prefer correcting the rate that looks like a rounding
     drift rather than inventing a brand-new value.
  3. Run the checker again to confirm.

Done when: `check_fx.py` exits 0 (all rates consistent).
Then output: <promise>CONSISTENT</promise>

Do not edit `seed/check_fx.py` — that is the gate, not the target.
Do not add new dependencies — the checker is pure standard library on purpose.
