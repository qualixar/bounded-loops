# CORS Not Wildcard: fix security_config.json so credentials never pair with '*'

Goal: make `python3 seed/check_cors.py seed/security_config.json` report no
violation (exit 0).

Steps each turn:
  1. Run the checker:
     `python3 seed/check_cors.py seed/security_config.json`
  2. If it flags `allow_credentials: true` combined with a wildcard `"*"` in
     `allow_origins`, remove the `"*"` entry from `allow_origins` in
     `seed/security_config.json`, keeping only the explicit, real origin(s)
     that must be allowed.
  3. Run the checker again to confirm.

Done when: `check_cors.py` exits 0 (no wildcard-origin-with-credentials violation).
Then output: <promise>VERIFIED</promise>

Do not edit `seed/check_cors.py` — that is the gate, not the target.
Do not "fix" this by setting `allow_credentials` to false unless that is
genuinely acceptable — prefer removing the wildcard and keeping the
explicit origin(s) so the service keeps working for its real client.
Do not add new dependencies — the checker is pure standard library on purpose.
