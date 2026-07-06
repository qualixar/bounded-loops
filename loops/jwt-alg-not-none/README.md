# jwt-alg-not-none

**Role:** security · **Rung:** L1 · **Gate:** `command` (keyless) · **Runner:** stub (keyless)

A bounded loop that drives an agent until a JWT config **never sets the
signing algorithm to `none`**. This is the runnable form of the classic
"alg: none" JWT bypass (CVE-class, present in nearly every JWT library's
history): an attacker crafts a token with `"alg": "none"` and no signature,
and a server that trusts the header's declared algorithm accepts it as
valid, forging arbitrary claims.

## What it demonstrates

The seed `auth_config.json` sets `jwt.algorithm` to `"none"` — accepting
unsigned tokens outright.

The gate `seed/check_jwt.py` fails whenever `jwt.algorithm` is
none/empty/None (case-insensitive). The loop is DONE only when the checker
exits 0.

## Run it (keyless, ~1s)

```bash
bl run loops/jwt-alg-not-none --yes   # stub runner + real command gate
```

You'll see the ungated config fail the checker, the recorded fix set the
algorithm to `"RS256"`, then the gate pass.

## Why the checker is `forbid:`-protected

The whole point is that the agent conforms the *config* to reality. Letting
it "fix" the failure by editing the checker to stop rejecting `none` would
fake a green gate. The engine refuses any write to `seed/check_jwt.py`.

## Make it real

Swap the stub runner for a real agent and point the checker at your actual
JWT library's config (PyJWT, jsonwebtoken, jose), or wrap a real
config-scanning tool behind the same command-gate contract. Keep the gate
as the bottleneck: an auth config is never "done" until its signing
algorithm is a genuine, non-forgeable one.
