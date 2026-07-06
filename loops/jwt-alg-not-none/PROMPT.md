# JWT Alg Not None: fix auth_config.json so the signing algorithm is never 'none'

Goal: make `python3 seed/check_jwt.py seed/auth_config.json` report a real
signing algorithm is set (exit 0).

Steps each turn:
  1. Run the checker:
     `python3 seed/check_jwt.py seed/auth_config.json`
  2. If it flags `jwt.algorithm` as none/empty/None, set it in
     `seed/auth_config.json` to a real signing algorithm (e.g. `"RS256"`).
  3. Run the checker again to confirm.

Done when: `check_jwt.py` exits 0 (algorithm is a real signing algorithm).
Then output: <promise>VERIFIED</promise>

Do not edit `seed/check_jwt.py` — that is the gate, not the target.
Do not remove the `algorithm` field entirely — it must be present and set
to a genuine signing algorithm.
Do not add new dependencies — the checker is pure standard library on purpose.
