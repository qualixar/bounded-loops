# dockerfile-no-root

**Role:** security · **Rung:** L1 · **Gate:** `command` (keyless) · **Runner:** stub (keyless)

A bounded loop that drives an agent until a Dockerfile **pins its base
image and drops root before running**. This is the runnable form of two of
the most common container misconfigurations: an unpinned `:latest` base
that can silently change under you, and a container that runs its process
as root inside the image.

## What it demonstrates

The seed `Dockerfile` has two problems:

- `FROM python:latest` — floats on whatever `latest` resolves to at build
  time, not an explicit, reproducible version.
- No `USER` instruction at all — the container runs as root by default.

The gate `seed/check_dockerfile.py` requires a pinned, non-`:latest` base
image and a `USER` instruction whose value is not `root`/`0`. The loop is
DONE only when the checker exits 0.

## Run it (keyless, ~1s)

```bash
bl run loops/dockerfile-no-root --yes   # stub runner + real command gate
```

You'll see the ungated Dockerfile fail the checker, the recorded fix pin
the base image and add a non-root `USER`, then the gate pass.

## Why the checker is `forbid:`-protected

The whole point is that the agent conforms the *Dockerfile* to reality.
Letting it "fix" the failure by editing the checker to stop requiring a
pinned tag or a `USER` line would fake a green gate. The engine refuses any
write to `seed/check_dockerfile.py`.

## Make it real

Swap the stub runner for a real agent and wrap a real linter (hadolint,
trivy config scan) behind the same command-gate contract. Keep the gate as
the bottleneck: a Dockerfile is never "done" until its base is pinned and
its process runs unprivileged.
