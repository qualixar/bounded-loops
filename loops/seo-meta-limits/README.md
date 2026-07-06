# seo-meta-limits

**Role:** content · **Rung:** L1 · **Gate:** `command` (keyless) · **Runner:** stub (keyless)

A bounded loop that drives an agent until **a page's SEO title and
description metadata both fit within the character limits search engines
actually truncate at** (~60 chars for title, ~160 for description).
Overflowing metadata gets cut off with an ellipsis in real search
results — a well-documented, widely-cited SEO failure.

## What it demonstrates

The seed `meta.json` ships a 74-character title (14 over the 60 limit)
and a 190-character description (30 over the 160 limit).

The gate `seed/check_meta.py` measures both fields against their limits
and flags any violation. The loop is DONE only when the checker exits 0.

## Run it (keyless, ~1s)

```bash
bl run loops/seo-meta-limits --yes   # stub runner + real command gate
```

You'll see the ungated metadata fail the checker on both fields, the
recorded fix trim the title to 60 chars and the description to 145 chars
while keeping the meaning, then the gate pass.

## Why the checker is `forbid:`-protected

The whole point is that the agent conforms the *metadata* to the real
limit. Editing the checker to raise `MAX_TITLE_CHARS` or
`MAX_DESCRIPTION_CHARS` would fake a green gate — exactly the "agent
talks its way past the verifier" failure bounded-loops exists to prevent.
The engine refuses any write to `seed/check_meta.py`.

## Make it real

Swap the stub runner for a real agent and point the checker at your CMS's
meta-tag export or static-site frontmatter — this same script works
unmodified on any page's title/description pair. Keep the gate as the
bottleneck: metadata is never "done" until it's independently verified to
fit within search-result display limits.
