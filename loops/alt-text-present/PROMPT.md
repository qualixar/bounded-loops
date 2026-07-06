# Alt Text Present: fix post.md so every image has alt text

Goal: make `python3 seed/check_alt.py seed/post.md` report that every
markdown image has non-empty alt text (exit 0).

Steps each turn:
  1. Run the checker:
     `python3 seed/check_alt.py seed/post.md`
  2. For each image it flags as missing alt text, look at the surrounding
     prose and the image filename to infer what the image actually shows,
     then write concise, descriptive alt text for it (not the filename,
     not a placeholder like "image").
  3. Run the checker again to confirm.

Done when: `check_alt.py` exits 0 (every image has non-empty alt text).
Then output: <promise>VERIFIED</promise>

Do not edit `seed/check_alt.py` — that is the gate, not the target.
Do not fill alt text with a filler word like "image" or "photo"; write a
real description of what the image shows.
Do not add new dependencies — the checker is pure standard library on purpose.
