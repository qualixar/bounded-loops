# reading-level-gate

**Role:** content · **Rung:** L1 · **Gate:** `command` (keyless) · **Runner:** stub (keyless)

A bounded loop that drives an agent until **a post's average
words-per-sentence drops to a readable level (<= 25)**. Average sentence
length is a cheap, dependency-free proxy for run-on complexity: long
average sentences correlate strongly with reader drop-off and
comprehension loss, well before you need a full Flesch-Kincaid formula.

## What it demonstrates

The seed `post.md` has two extremely long, clause-stacked run-on sentences
that push the average words-per-sentence to 43.8 — nearly double the
25-word limit.

The gate `seed/check_readability.py` splits the prose into sentences,
counts words per sentence, and fails if the average exceeds 25. The loop
is DONE only when the checker exits 0.

## Run it (keyless, ~1s)

```bash
bl run loops/reading-level-gate --yes   # stub runner + real command gate
```

You'll see the ungated post fail the checker at 43.8 words/sentence, the
recorded fix split both run-ons into shorter sentences (no content
deleted), then the gate pass at 16.5 words/sentence.

## Why the checker is `forbid:`-protected

The whole point is that the agent conforms the *prose* to a readable
standard. Editing the checker to raise the 25-word threshold would fake a
green gate — exactly the "agent talks its way past the verifier" failure
bounded-loops exists to prevent. The engine refuses any write to
`seed/check_readability.py`.

## Make it real

Swap the stub runner for a real agent and point the checker at your CMS
draft queue or static-site `content/` tree — this same script works
unmodified on any markdown post. Keep the gate as the bottleneck: a post
is never "done" until its average sentence length is independently
verified to be readable.
