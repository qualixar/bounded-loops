# Contract Defined Terms: fix contract.md so every bolded term is defined

Goal: make `python3 seed/check_defined.py seed/contract.md` report that
every bolded term used in the body is defined (exit 0).

Steps each turn:
  1. Run the checker:
     `python3 seed/check_defined.py seed/contract.md`
  2. For each term it flags as used but not defined, add a new bullet to
     the "## Definitions" section of `seed/contract.md` in the same style
     as the existing entries (e.g. `- **"Effective Date"** means ...`),
     giving it a clear, sensible meaning consistent with how the term is
     used in the body.
  3. Run the checker again to confirm.

Done when: `check_defined.py` exits 0 (every bolded body term is defined).
Then output: <promise>VERIFIED</promise>

Do not edit `seed/check_defined.py` — that is the gate, not the target.
Do not unbold or remove any existing usage of a term in the body — the
fix is to ADD the missing definition, never to delete the usage.
Do not add new dependencies — the checker is pure standard library on
purpose.
