# CDS View Annotations: fix zcds_view.txt so the required annotations are present

Goal: make `python3 seed/check_cds.py seed/zcds_view.txt` report that all
required annotations are present (exit 0).

Steps each turn:
  1. Run the checker:
     `python3 seed/check_cds.py seed/zcds_view.txt`
  2. For each annotation it flags as missing, open `seed/zcds_view.txt`
     and add that annotation line near the other `@...` annotations at
     the top of the view, with a sensible value consistent with the
     rest of the view.
  3. Run the checker again to confirm.

Done when: `check_cds.py` exits 0 (`@AccessControl.authorizationCheck`,
`@EndUserText.label`, and `@Metadata.allowExtensions` are all present).
Then output: <promise>VERIFIED</promise>

Do not edit `seed/check_cds.py` — that is the gate, not the target.
Do not remove or rename the view's fields to avoid the requirement —
the view's select list is out of scope; add only the missing annotations.
Do not add new dependencies — the checker is pure standard library on
purpose.
