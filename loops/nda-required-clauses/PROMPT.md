# NDA Required Clauses: fix nda.md so every required clause is present

Goal: make `python3 seed/check_clauses.py seed/nda.md` report that every
required clause is present (exit 0).

Steps each turn:
  1. Run the checker:
     `python3 seed/check_clauses.py seed/nda.md`
  2. For each clause it flags as missing, add a new section to
     `seed/nda.md` with an appropriate heading and body text covering
     that clause (e.g. a "Governing Law" section naming the governing
     jurisdiction, or a "Return of Materials" section requiring return
     or destruction of Confidential Information on request or
     termination).
  3. Run the checker again to confirm.

Done when: `check_clauses.py` exits 0 (every required clause is present).
Then output: <promise>VERIFIED</promise>

Do not edit `seed/check_clauses.py` — that is the gate, not the target.
Do not delete or weaken any existing clause in `seed/nda.md` to make the
checker's job easier — only ADD the missing required sections.
Do not add new dependencies — the checker is pure standard library on
purpose.
