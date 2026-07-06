# PRD Acceptance Criteria: fix prd.md so every story is verifiable

Goal: make `python3 seed/check_prd.py seed/prd.md` report that every
`## Story:` section has an Acceptance Criteria subsection with at least one
bullet (exit 0).

Steps each turn:
  1. Run the checker:
     `python3 seed/check_prd.py seed/prd.md`
  2. For each story it flags as missing acceptance criteria, open
     `seed/prd.md` and add a `### Acceptance Criteria` subsection with at
     least one checkbox/bullet item that a QA engineer could actually test
     against — written to match what the story already describes, not a
     placeholder.
  3. Run the checker again to confirm.

Done when: `check_prd.py` exits 0 (every story has acceptance criteria).
Then output: <promise>VERIFIABLE</promise>

Do not delete a story to dodge the check — fix it by adding real acceptance
criteria.
Do not edit `seed/check_prd.py` — that is the gate, not the target.
Do not add new dependencies — the checker is pure standard library on purpose.
