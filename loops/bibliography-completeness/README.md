# bibliography-completeness

**Role:** research · **Rung:** L1 · **Gate:** `command` (keyless) · **Runner:** stub (keyless)

A bounded loop that drives an agent until **every pandoc-style inline
citation key in a paper's body appears as a listed key in its own References
section**. This is the runnable form of a common AI-assisted-drafting
failure: a citation key that was typo'd or never added to the bibliography.

## What it demonstrates

The seed `paper.md` makes three claims with `[@key]` markers. One is wrong:

- **`[@jones2021]`** on the memory-staleness sentence — the `## References`
  section only lists `smith2020` and `lee2019`. The claim is really the
  finding of `lee2019` (the versioned-state-stores paper) — a typo'd key,
  not a wholly fabricated claim.

The gate `seed/check_biblio.py` splits the document at its own
`## References` header, collects the keys listed there, and flags any
`[@key]` citation in the body that is not among them. The loop is DONE only
when the checker exits 0.

## Run it (keyless, ~1s)

```bash
bl run loops/bibliography-completeness --yes   # stub runner + real command gate
```

You'll see the ungated paper fail the checker, the recorded fix correct
`[@jones2021]` to `[@lee2019]`, then the gate pass.

## Why the checker is `forbid:`-protected

The whole point is that the agent conforms the *citation key* to what is
actually listed. Letting it "fix" the failure by adding a fabricated
`jones2021` entry to References, or by editing the checker, would fake a
green gate — exactly the "agent talks its way past the verifier" failure
bounded-loops exists to prevent. The engine refuses any write to
`seed/check_biblio.py`.

## Make it real

Swap the stub runner for a real agent and point the checker at your actual
`.bib`/reference-manager export instead of a hand-listed References section.
Keep the gate as the bottleneck: a paper is never "done" until every cited
key resolves to a real, listed reference.

## Which Anthropic pattern

`evaluator-optimizer` — the gate (`check_biblio.py`) is the evaluator; the
agent-turn is the optimizer. The loop runs until the evaluator says every
citation key is listed.
Reference: [Anthropic — Building effective agents](https://www.anthropic.com/research/building-effective-agents)
