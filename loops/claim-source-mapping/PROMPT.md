# Claim-Source Mapping: fix article.md so every citation marker is real

Goal: make `python3 seed/check_claims.py seed/article.md seed/sources.json`
report that every `[S#]` citation marker resolves to a real source id
(exit 0).

Steps each turn:
  1. Run the checker:
     `python3 seed/check_claims.py seed/article.md seed/sources.json`
  2. For each citation marker it flags as "not found in sources.json", open
     `seed/sources.json` (read-only ground truth) and decide:
     - the claim is really supported by a source that IS in the list, just
       cited under the wrong number → correct the marker in `seed/article.md`
       to the real source id;
     - the claim has no real supporting source in the list at all → remove
       that sentence/claim from `seed/article.md`.
  3. Run the checker again to confirm.

Done when: `check_claims.py` exits 0 (every citation marker is real).
Then output: <promise>VERIFIED</promise>

Do not edit `seed/sources.json` — the source list is the ground truth, not
something to expand to make a fabricated citation "real".
Do not edit `seed/check_claims.py` — that is the gate, not the target.
Do not invent a new source id to replace a bad one; if a claim has no real
supporting source in the list, drop the claim's citation and the claim itself.
Do not add new dependencies — the checker is pure standard library on purpose.
