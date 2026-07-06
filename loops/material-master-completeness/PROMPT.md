# Material Master Completeness: fix material.json so required fields are present

Goal: make `python3 seed/check_material.py seed/material.json` report that
all fields required for this material's type are present (exit 0).

Steps each turn:
  1. Run the checker:
     `python3 seed/check_material.py seed/material.json`
  2. For each field it flags as missing or empty, open
     `seed/material.json` and fill it in with a plausible value
     consistent with the rest of the record (e.g. a real-looking sales
     division code for a finished product).
  3. Run the checker again to confirm.

Done when: `check_material.py` exits 0 (every field required for this
`material_type` — FERT needs base_uom/description/division, ROH needs
base_uom/description — is present and non-empty).
Then output: <promise>VERIFIED</promise>

Do not edit `seed/check_material.py` — that is the gate, not the target.
Do not change `material_type` to a type with fewer required fields to
dodge the requirement — fill in the missing field instead.
Do not add new dependencies — the checker is pure standard library on
purpose.
