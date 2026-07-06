# Privacy Policy Completeness: fix privacy.md so every required section is present

Goal: make `python3 seed/check_privacy.py seed/privacy.md` report that
every required section is present (exit 0).

Steps each turn:
  1. Run the checker:
     `python3 seed/check_privacy.py seed/privacy.md`
  2. For each section it flags as missing, add a new section to
     `seed/privacy.md` with an appropriate heading and body text covering
     that topic (e.g. a "Data Retention" section stating how long data is
     kept, or a "Your Rights" section describing access, correction, and
     deletion rights available to individuals).
  3. Run the checker again to confirm.

Done when: `check_privacy.py` exits 0 (every required section is
present). Then output: <promise>VERIFIED</promise>

Do not edit `seed/check_privacy.py` — that is the gate, not the target.
Do not delete or weaken any existing section in `seed/privacy.md` to make
the checker's job easier — only ADD the missing required sections.
Do not add new dependencies — the checker is pure standard library on
purpose.
