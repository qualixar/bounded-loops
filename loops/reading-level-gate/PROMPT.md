# Reading Level Gate: fix post.md so the average sentence isn't a run-on

Goal: make `python3 seed/check_readability.py seed/post.md` report that
the average words-per-sentence is at or below 25 (exit 0).

Steps each turn:
  1. Run the checker:
     `python3 seed/check_readability.py seed/post.md`
  2. Find the longest, most clause-stacked sentences in `seed/post.md` and
     split each one into two or more shorter sentences that preserve every
     idea in the original — do not delete or summarize away content, just
     break run-ons at natural clause boundaries.
  3. Run the checker again to confirm.

Done when: `check_readability.py` exits 0 (average words/sentence <= 25).
Then output: <promise>VERIFIED</promise>

Do not edit `seed/check_readability.py` — that is the gate, not the target.
Do not delete sentences or ideas to game the average; split long sentences
into shorter ones instead, keeping the same meaning.
Do not add new dependencies — the checker is pure standard library on purpose.
