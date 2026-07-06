# dependency-pinning

**Role:** security · **Rung:** L1 · **Gate:** `command` (keyless) · **Runner:** stub (keyless)

A bounded loop that drives an agent until **every dependency is pinned to an
exact version**. This is the runnable form of a common supply-chain
weakness: an unpinned or ranged dependency that can silently resolve to a
new, unreviewed, possibly-compromised release at install time.

## What it demonstrates

The seed `requirements.txt` lists four dependencies, two of which are not
exactly pinned:

- `flask>=2.3.0` — an open-ended range, not an exact pin.
- `numpy` — a bare name with no version constraint at all.

The gate `seed/check_pins.py` requires every non-comment, non-blank line to
match `package==version` exactly. The loop is DONE only when the checker
exits 0.

## Run it (keyless, ~1s)

```bash
bl run loops/dependency-pinning --yes   # stub runner + real command gate
```

You'll see the ungated requirements file fail the checker, the recorded fix
pin both dependencies to exact versions, then the gate pass.

## Why the checker is `forbid:`-protected

The whole point is that the agent conforms the *requirements file* to
reality. Letting it "fix" the failure by editing the checker to accept
ranges would fake a green gate. The engine refuses any write to
`seed/check_pins.py`.

## Make it real

Swap the stub runner for a real agent and point the checker at your actual
lockfile convention (poetry.lock, Pipfile.lock, package-lock.json), or wrap
a real SCA tool (osv-scanner, pip-audit) behind the same command-gate
contract. Keep the gate as the bottleneck: a requirements file is never
"done" until every line is an exact, reproducible pin.
