# Dependency Pinning: fix requirements.txt so every dependency is exactly pinned

Goal: make `python3 seed/check_pins.py seed/requirements.txt` report every
dependency is pinned to an exact version (exit 0).

Steps each turn:
  1. Run the checker:
     `python3 seed/check_pins.py seed/requirements.txt`
  2. For each unpinned dependency it flags, replace the range or bare name
     in `seed/requirements.txt` with an exact `package==version` pin using a
     concrete, currently-real release version.
  3. Run the checker again to confirm.

Done when: `check_pins.py` exits 0 (every dependency is exactly pinned).
Then output: <promise>VERIFIED</promise>

Do not edit `seed/check_pins.py` — that is the gate, not the target.
Do not delete a dependency to make it pass — every dependency must remain,
just pinned to an exact version.
Do not add new dependencies — the checker is pure standard library on purpose.
