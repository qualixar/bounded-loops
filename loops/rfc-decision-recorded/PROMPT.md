# RFC Decision Recorded: fix rfc.md so the decision is actually written down

Goal: make `python3 seed/check_rfc.py seed/rfc.md` report that Status,
Context, Decision, and Consequences are all present (exit 0).

Steps each turn:
  1. Run the checker:
     `python3 seed/check_rfc.py seed/rfc.md`
  2. For each section it flags as missing, open `seed/rfc.md` and add it:
     - `## Decision` — state which option was actually chosen, consistent
       with the `Status` and the options already discussed in `Context`;
     - `## Consequences` — state the real tradeoffs and follow-up work
       that choosing that option implies (what gets harder, what gets
       easier, what has to be migrated).
  3. Run the checker again to confirm.

Done when: `check_rfc.py` exits 0 (Status, Context, Decision, Consequences
all present).
Then output: <promise>RECORDED</promise>

Do not just add empty headings — the Decision and Consequences sections must
contain real content consistent with the Context already in the document.
Do not edit `seed/check_rfc.py` — that is the gate, not the target.
Do not add new dependencies — the checker is pure standard library on purpose.
