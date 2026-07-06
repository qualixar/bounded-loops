# Conventional Commits: fix commits.txt so every subject conforms

Goal: make `python3 seed/check_commits.py seed/commits.txt` report that
every commit subject conforms to Conventional Commits (exit 0).

Steps each turn:
  1. Run the checker:
     `python3 seed/check_commits.py seed/commits.txt`
  2. For each line it flags as malformed, rewrite that line in
     `seed/commits.txt` to `type(optional-scope): description`, choosing
     the type (feat, fix, docs, refactor, test, chore, perf, ci, build)
     that best matches what the change actually did — preserve the
     original meaning of the subject, don't just relabel it arbitrarily.
  3. Run the checker again to confirm.

Done when: `check_commits.py` exits 0 (every subject conforms).
Then output: <promise>CONFORMANT</promise>

Do not edit `seed/check_commits.py` — that is the gate, not the target.
Do not delete a malformed line to dodge the check; rewrite it to conform
while keeping its original meaning.
Do not add new dependencies — the checker is pure standard library on
purpose.
