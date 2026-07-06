# Bibliography Completeness: fix paper.md so every cited key is listed

Goal: make `python3 seed/check_biblio.py seed/paper.md` report that every
`[@key]` citation in the body appears as a listed key under `## References`
(exit 0).

Steps each turn:
  1. Run the checker:
     `python3 seed/check_biblio.py seed/paper.md`
  2. For each citation key it flags as "not found in References", look at the
     `## References` section of `seed/paper.md` and decide:
     - the claim is really supported by a reference that IS listed, just
       cited under a typo'd/wrong key → correct the key in the body to match
       the real listed key;
     - the claim has no real supporting reference listed at all → remove
       that sentence/citation from the body.
  3. Run the checker again to confirm.

Done when: `check_biblio.py` exits 0 (every cited key is listed).
Then output: <promise>VERIFIED</promise>

Do not edit the `## References` section to add a new entry just to make a
fabricated citation "real" — References is the ground truth for what is
actually listed.
Do not edit `seed/check_biblio.py` — that is the gate, not the target.
Do not invent a new reference key; if a claim has no real listed support,
drop the citation and the claim.
Do not add new dependencies — the checker is pure standard library on purpose.
