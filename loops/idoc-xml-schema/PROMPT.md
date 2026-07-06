# IDoc XML Schema: fix idoc.xml so the structural gate passes

Goal: make `python3 seed/check_idoc.py seed/idoc.xml` report that the IDoc
is structurally valid (exit 0).

Steps each turn:
  1. Run the checker:
     `python3 seed/check_idoc.py seed/idoc.xml`
  2. For each element it flags as missing or empty, open `seed/idoc.xml`
     and add the missing element in the correct segment, using a
     plausible value consistent with the surrounding document.
  3. Run the checker again to confirm.

Done when: `check_idoc.py` exits 0 (control segment complete, header
present, and at least one item segment carries a non-empty quantity).
Then output: <promise>VERIFIED</promise>

Do not edit `seed/check_idoc.py` — that is the gate, not the target.
Do not delete the item segment `<E1EDP01>` to dodge the requirement —
add the missing `<MENGE>` quantity to it instead.
Do not add new dependencies — the checker is pure standard library
(`xml.etree.ElementTree`) on purpose.
