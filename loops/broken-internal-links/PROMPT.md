# Broken Internal Links: fix content/ so every relative link resolves

Goal: make `python3 seed/check_links.py seed/content` report that every
relative internal markdown link resolves to a real file (exit 0).

Steps each turn:
  1. Run the checker:
     `python3 seed/check_links.py seed/content`
  2. For each link it flags as broken, open the file that contains it and
     decide the correct target:
     - if the intended page exists under `content/` with a different name,
       repoint the link at that real file;
     - if there truly is no matching page, remove the link (keep the
       surrounding prose) rather than leave it dangling.
  3. Run the checker again to confirm.

Done when: `check_links.py` exits 0 (every relative internal link resolves).
Then output: <promise>VERIFIED</promise>

Do not edit `seed/check_links.py` — that is the gate, not the target.
Do not create a new empty file just to make a broken link resolve; repoint
or remove the link instead.
Do not add new dependencies — the checker is pure standard library on purpose.
