# Transport Request Manifest: fix transport.json so no dependency dangles

Goal: make `python3 seed/check_transport.py seed/transport.json` report
that every dependency is covered by an object in the request (exit 0).

Steps each turn:
  1. Run the checker:
     `python3 seed/check_transport.py seed/transport.json`
  2. For each dangling dependency it flags, open `seed/transport.json` and
     add that id to the `objects` array — the transport request must
     actually carry every object it declares a dependency on.
  3. Run the checker again to confirm.

Done when: `check_transport.py` exits 0 (no dependency is dangling).
Then output: <promise>VERIFIED</promise>

Do not edit `seed/check_transport.py` — that is the gate, not the target.
Do not remove the dangling id from `dependencies` to dodge the requirement
— add it to `objects` instead; the request genuinely needs to carry every
object it depends on to import cleanly downstream.
Do not add new dependencies — the checker is pure standard library on
purpose.
