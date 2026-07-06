# conventional-commits

**Role:** operations · engineering · **Rung:** L1 · **Gate:** `command` (keyless) · **Runner:** stub (keyless)

A bounded loop that drives an agent until **every commit subject in a list
conforms to Conventional Commits**. This is the runnable form of a common
release-engineering failure: a free-text or "WIP ..." commit subject that
slips past review and then silently breaks changelog generation or a
semantic-release version bump that parses commit history.

## What it demonstrates

The seed `commits.txt` has seven subjects; five conform and two don't:

- **"Updated the readme with new setup instructions"** — no `type:` prefix
  at all.
- **"WIP fix login bug"** — a work-in-progress marker, not a Conventional
  Commits type.

The gate `seed/check_commits.py` matches every line against
`^(feat|fix|docs|refactor|test|chore|perf|ci|build)(\([a-z0-9-]+\))?: .+`
and flags any that don't match. The loop is DONE only when the checker
exits 0.

## Run it (keyless, ~1s)

```bash
bl run loops/conventional-commits --yes   # stub runner + real command gate
```

You'll see the two malformed subjects fail the checker, the recorded fix
rewrite them as `docs(readme): ...` and `fix(auth): ...` while preserving
their original meaning, then the gate pass.

## Why the checker is `forbid:`-protected

The whole point is that the agent conforms the *subjects* to the
convention, not the convention to the subjects. Letting it "fix" the
failure by editing the checker's regex to accept anything would fake a
green gate — exactly the "agent talks its way past the verifier" failure
bounded-loops exists to prevent. The engine refuses any write to
`seed/check_commits.py`.

## Make it real

Point the checker at `git log --format=%s` for a real branch and run it as
a commit-msg hook or CI check on every PR, so a malformed subject never
reaches main.
