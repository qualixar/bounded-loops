# OSV Scan: remove the known-vulnerable dependency pin

Goal: make `osv-scanner scan --format json --recursive .` report zero
known vulnerabilities for `seed/package-lock.json`.

Steps each turn:
  1. Read `seed/package-lock.json` and note the pinned `minimatch` version.
  2. `minimatch@3.0.4` carries multiple real, disclosed CVEs (ReDoS class
     vulnerabilities). Bump the pin to a version at or above the fixed
     range for the 0.x-3.x line (`3.1.4` clears all four advisories).
  3. Update BOTH the top-level `dependencies` entry and the
     `node_modules/minimatch` entry's `version`/`resolved` fields so the
     lockfile stays internally consistent.

Done when: `osv-scanner scan --format json --recursive .` exits 0 (no
known vulnerabilities).

Do not delete `package-lock.json`.
Do not add new dependencies.
