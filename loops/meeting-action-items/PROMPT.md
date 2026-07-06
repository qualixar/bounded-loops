# Meeting Action Items: fix minutes.md so every action has an owner and a date

Goal: make `python3 seed/check_actions.py seed/minutes.md` report that
every action item under "## Action Items" names an owner (`@name`) and a
due date (`YYYY-MM-DD`) (exit 0).

Steps each turn:
  1. Run the checker:
     `python3 seed/check_actions.py seed/minutes.md`
  2. For each action item it flags, open `seed/minutes.md` and add
     whatever is missing:
     - an `@name` owner tag, consistent with who was in the meeting or
       who the item logically belongs to;
     - a due date in `YYYY-MM-DD` form, consistent with the meeting date
       and the other action items already dated.
  3. Run the checker again to confirm.

Done when: `check_actions.py` exits 0 (every action item has an owner and
a due date).
Then output: <promise>ASSIGNED</promise>

Do not delete an action item to dodge the check — fix it so it is
genuinely assigned and dated.
Do not edit `seed/check_actions.py` — that is the gate, not the target.
Do not add new dependencies — the checker is pure standard library on purpose.
