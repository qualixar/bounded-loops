# alt-text-present

**Role:** content · **Rung:** L1 · **Gate:** `command` (keyless) · **Runner:** stub (keyless)

A bounded loop that drives an agent until **every markdown image in a post
has non-empty alt text**. This is the runnable form of one of the most
common, most overlooked accessibility and SEO failures: an image ships
with `![]()`, so screen readers announce nothing and image search has
nothing to index.

## What it demonstrates

The seed `post.md` has three images: one with a real alt description
(passes), and two with empty `![]()` alt text (fail).

The gate `seed/check_alt.py` extracts every markdown image and flags any
whose alt-text group is empty or whitespace-only. The loop is DONE only
when the checker exits 0.

## Run it (keyless, ~1s)

```bash
bl run loops/alt-text-present --yes   # stub runner + real command gate
```

You'll see the ungated post fail the checker, the recorded fix add real
descriptive alt text to both images, then the gate pass.

## Why the checker is `forbid:`-protected

The whole point is that the agent conforms the *content* to reality.
Editing the checker to stop requiring alt text would fake a green gate —
exactly the "agent talks its way past the verifier" failure bounded-loops
exists to prevent. The engine refuses any write to `seed/check_alt.py`.

## Make it real

Swap the stub runner for a real agent and point the checker at your CMS
export or static-site `content/` tree — this same script works unmodified
on any markdown corpus. Keep the gate as the bottleneck: a post is never
"done" until every image is independently verified to carry real alt text.
