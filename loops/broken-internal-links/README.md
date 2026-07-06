# broken-internal-links

**Role:** content · **Rung:** L1 · **Gate:** `command` (keyless) · **Runner:** stub (keyless)

A bounded loop that drives an agent until **every relative internal
markdown link in a content directory resolves to a real file on disk**.
This is the runnable form of the most common silent content-QA failure: a
reader clicks "read the introduction" and hits a 404.

## What it demonstrates

The seed `content/a.md` links to two targets: `b.md` (real, exists) and
`missing.md` (does not exist anywhere under `content/`).

The gate `seed/check_links.py` walks every markdown file under
`seed/content`, extracts relative link targets, and flags any that don't
resolve to a real file. The loop is DONE only when the checker exits 0.

## Run it (keyless, ~1s)

```bash
bl run loops/broken-internal-links --yes   # stub runner + real command gate
```

You'll see the ungated content fail the checker, the recorded fix repoint
the broken link at the real `b.md`, then the gate pass.

## Why the checker is `forbid:`-protected

The whole point is that the agent conforms the *content* to reality.
Editing the checker to stop looking for broken links, or to whitelist
`missing.md`, would fake a green gate — exactly the "agent talks its way
past the verifier" failure bounded-loops exists to prevent. The engine
refuses any write to `seed/check_links.py`.

## Make it real

Swap the stub runner for a real agent and point the checker at your real
`content/` or `docs/` tree — this same script works unmodified on any
static-site or docs-as-code repo. Keep the gate as the bottleneck: content
is never "done" until every internal link is independently verified to
resolve.
