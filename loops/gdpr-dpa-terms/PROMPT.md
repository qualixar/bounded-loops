# GDPR DPA Terms: fix dpa.md so every Art.28(3) mandatory term is present

Goal: make `python3 seed/check_dpa.py seed/dpa.md` report that every
mandatory GDPR Article 28(3) term is present (exit 0).

Steps each turn:
  1. Run the checker:
     `python3 seed/check_dpa.py seed/dpa.md`
  2. For each term it flags as missing, add a new section to
     `seed/dpa.md` with an appropriate heading and body text covering
     that term (e.g. a "Sub-Processor" section on engagement and
     flow-down of obligations to sub-processors, or an "Audit" section
     granting the controller audit/inspection rights).
  3. Run the checker again to confirm.

Done when: `check_dpa.py` exits 0 (every mandatory term is present).
Then output: <promise>VERIFIED</promise>

Do not edit `seed/check_dpa.py` — that is the gate, not the target.
Do not delete or weaken any existing term in `seed/dpa.md` to make the
checker's job easier — only ADD the missing mandatory terms.
Do not add new dependencies — the checker is pure standard library on
purpose.
