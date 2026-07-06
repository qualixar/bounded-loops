# Dataset License Allowed: fix datasets.json so every license is cleared

Goal: make `python3 seed/check_licenses.py seed/datasets.json seed/allowlist.json`
report that every dataset's license is in the allowlist (exit 0).

Steps each turn:
  1. Run the checker:
     `python3 seed/check_licenses.py seed/datasets.json seed/allowlist.json`
  2. For each dataset it flags as having a disallowed license, open
     `seed/allowlist.json` (read-only ground truth) and decide:
     - the dataset's license was mistyped/mislabeled and the real license is
       actually one already in the allowlist → correct the `license` field
       in `seed/datasets.json` to the real, accurate license;
     - the dataset's true license genuinely is not in the allowlist (e.g. a
       copyleft license like GPL-3.0 with no permissive equivalent) → remove
       that dataset entry from `seed/datasets.json` entirely. Never use
       disallowed-license data by relabeling it.
  3. Run the checker again to confirm.

Done when: `check_licenses.py` exits 0 (every remaining dataset's license is
allowed).
Then output: <promise>VERIFIED</promise>

Do not edit `seed/allowlist.json` — the allowlist is the ground truth, not
something to expand to make a disallowed license "pass".
Do not edit `seed/check_licenses.py` — that is the gate, not the target.
Do not relabel a dataset's actual license to something allowed just to pass
the gate; if the true license is disallowed, drop the dataset.
Do not add new dependencies — the checker is pure standard library on purpose.
