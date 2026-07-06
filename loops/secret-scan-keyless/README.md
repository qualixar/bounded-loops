# secret-scan-keyless

**Role:** security · **Rung:** L1 · **Gate:** `command` (keyless) · **Runner:** stub (keyless)

A bounded loop that drives an agent until **no secret is hardcoded** in a
config file. This is the runnable form of the most common source-level
security defect: a committed AWS key or plaintext password.

## What it demonstrates

The seed `app_config.py` hardcodes two secrets:

- `AWS_ACCESS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"` — a real-shaped AWS access
  key id literal.
- `password = "Sup3rSecretPass!"` — a plaintext admin password.

The gate `seed/check_secrets.py` scans the file with pure-stdlib regexes for
AWS key ids, a PEM private-key header, and password/api_key/secret literal
assignments. The loop is DONE only when the checker exits 0.

## Run it (keyless, ~1s)

```bash
bl run loops/secret-scan-keyless --yes   # stub runner + real command gate
```

You'll see the ungated config fail the checker, the recorded fix move both
secrets to `os.environ[...]` reads, then the gate pass.

## Why the checker is `forbid:`-protected

The whole point is that the agent conforms the *config* to reality. Letting
it "fix" the failure by editing the checker to stop scanning would fake a
green gate. The engine refuses any write to `seed/check_secrets.py`.

## Make it real

Swap the stub runner for a real agent and point the checker at your actual
service's env-loading convention, or wrap a real secret scanner (gitleaks,
trivy) behind the same command-gate contract. Keep the gate as the
bottleneck: a config is never "done" until it holds zero literal secrets.
