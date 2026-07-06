# Dockerfile No-Root: fix Dockerfile so it pins its base and drops root

Goal: make `python3 seed/check_dockerfile.py seed/Dockerfile` report both
checks pass (exit 0).

Steps each turn:
  1. Run the checker:
     `python3 seed/check_dockerfile.py seed/Dockerfile`
  2. For each violation it flags:
     - a `:latest`-tagged or untagged `FROM` image → pin it to an explicit
       version tag (e.g. `python:3.12-slim`);
     - no non-root `USER` instruction → add one before `CMD`, e.g.
       `RUN useradd -m app` then `USER app`.
  3. Run the checker again to confirm.

Done when: `check_dockerfile.py` exits 0 (base image pinned, non-root USER set).
Then output: <promise>VERIFIED</promise>

Do not edit `seed/check_dockerfile.py` — that is the gate, not the target.
Do not set `USER root` or `USER 0` — that does not satisfy the requirement.
Do not add new dependencies — the checker is pure standard library on purpose.
