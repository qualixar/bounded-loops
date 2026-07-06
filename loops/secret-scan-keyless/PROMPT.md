# Secret Scan: fix app_config.py so no secret is hardcoded

Goal: make `python3 seed/check_secrets.py seed/app_config.py` report no
hardcoded secrets (exit 0).

Steps each turn:
  1. Run the checker:
     `python3 seed/check_secrets.py seed/app_config.py`
  2. For each finding, replace the hardcoded literal in `seed/app_config.py`
     with a read from the environment, e.g.
     `AWS_ACCESS_KEY_ID = os.environ["AWS_ACCESS_KEY_ID"]` and
     `password = os.environ["ADMIN_PASSWORD"]`. Add `import os` if needed.
  3. Run the checker again to confirm.

Done when: `check_secrets.py` exits 0 (no hardcoded secrets remain).
Then output: <promise>VERIFIED</promise>

Do not edit `seed/check_secrets.py` — that is the gate, not the target.
Do not simply delete the config values or the whole file — the config must
keep working, sourced from the environment instead of a literal.
Do not add new dependencies — the checker is pure standard library on purpose.
