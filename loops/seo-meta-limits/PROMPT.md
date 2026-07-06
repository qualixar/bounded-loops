# SEO Meta Limits: fix meta.json so title and description fit

Goal: make `python3 seed/check_meta.py seed/meta.json` report that both
`title` (<= 60 chars) and `description` (<= 160 chars) are non-empty and
within their limits (exit 0).

Steps each turn:
  1. Run the checker:
     `python3 seed/check_meta.py seed/meta.json`
  2. For each field it flags as too long, trim it down below the limit
     while preserving the core meaning — cut filler words and redundant
     phrasing rather than truncating mid-sentence.
  3. Run the checker again to confirm.

Done when: `check_meta.py` exits 0 (title <= 60 chars, description <= 160
chars, both non-empty).
Then output: <promise>VERIFIED</promise>

Do not edit `seed/check_meta.py` — that is the gate, not the target.
Do not truncate a field mid-word or mid-sentence; rewrite it as a shorter,
still-meaningful sentence.
Do not add new dependencies — the checker is pure standard library on purpose.
