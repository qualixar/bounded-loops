# cors-not-wildcard

**Role:** security · **Rung:** L1 · **Gate:** `command` (keyless) · **Runner:** stub (keyless)

A bounded loop that drives an agent until a CORS config **never combines a
wildcard origin with credentialed requests**. This is the runnable form of
a classic CORS misconfiguration: `Access-Control-Allow-Origin: *` paired
with `Access-Control-Allow-Credentials: true` lets any website read
authenticated responses from the API on a victim's behalf.

## What it demonstrates

The seed `security_config.json` sets:

- `allow_origins: ["https://app.example.com", "*"]` — includes the wildcard.
- `allow_credentials: true` — credentialed (cookie/auth-header) requests
  are allowed.

Together these are unsafe: browsers normally block wildcard origins from
carrying credentials, but misconfigured servers or proxies can still
reflect them, and the combination is flagged by every major CORS audit
tool. The gate `seed/check_cors.py` fails whenever both conditions hold.
The loop is DONE only when the checker exits 0.

## Run it (keyless, ~1s)

```bash
bl run loops/cors-not-wildcard --yes   # stub runner + real command gate
```

You'll see the ungated config fail the checker, the recorded fix drop the
wildcard from `allow_origins` while keeping the real origin, then the gate
pass.

## Why the checker is `forbid:`-protected

The whole point is that the agent conforms the *config* to reality. Letting
it "fix" the failure by editing the checker to stop checking this
combination would fake a green gate. The engine refuses any write to
`seed/check_cors.py`.

## Make it real

Swap the stub runner for a real agent and point the checker at your actual
framework's CORS middleware config (FastAPI's `CORSMiddleware`, Express's
`cors()` options, etc.), or wrap a real config-scanning tool behind the
same command-gate contract. Keep the gate as the bottleneck: a CORS config
is never "done" until wildcard origins and credentials are mutually
exclusive.
