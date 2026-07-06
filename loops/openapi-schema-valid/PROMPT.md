# OpenAPI Schema Valid: fix openapi.json so every operation has responses

Goal: make `python3 seed/check_openapi.py seed/openapi.json` report that the
document satisfies the minimal OpenAPI 3 contract (exit 0).

Steps each turn:
  1. Run the checker:
     `python3 seed/check_openapi.py seed/openapi.json`
  2. For each operation it flags as missing `responses`, open
     `seed/openapi.json` and add a `responses` object describing at least
     one real outcome (e.g. a `"201"` or `"200"` entry with a `description`).
  3. Run the checker again to confirm.

Done when: `check_openapi.py` exits 0 (every operation declares responses,
and `openapi`/`info.title`/`info.version` are all present).
Then output: <promise>VALID</promise>

Do not edit `seed/check_openapi.py` — that is the gate, not the target.
Do not remove the operation that is missing `responses` as a shortcut; add
the missing `responses` object to it instead.
Do not add new dependencies — the checker is pure standard library on purpose.
