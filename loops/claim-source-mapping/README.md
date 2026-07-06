# claim-source-mapping

**Role:** research · **Rung:** L1 · **Gate:** `command` (keyless) · **Runner:** stub (keyless)

A bounded loop that drives an agent until **every inline citation marker in a
research article resolves to a real source** in a trusted source list. This
is the runnable form of a common AI-research-writing failure: claim-source
drift, where a generated article cites a source number that was never in the
research set.

## What it demonstrates

The seed `article.md` makes four claims with `[S#]` markers. One is wrong:

- **`[S3]`** on the multi-agent-coordination sentence — `sources.json` only
  has `S1` and `S2`. The claim (removing a shared network bottleneck) is
  really supported by `S2`, the retrieval-augmented paper — a mis-numbered
  reference, not a wholly fabricated claim.

The gate `seed/check_claims.py` derives the set of valid source ids from
`sources.json` and flags any `[S#]` marker in the article body that is not a
real id. The loop is DONE only when the checker exits 0.

## Run it (keyless, ~1s)

```bash
bl run loops/claim-source-mapping --yes   # stub runner + real command gate
```

You'll see the ungated article fail the checker, the recorded fix correct
`[S3]` to `[S2]`, then the gate pass.

## Why the source list and checker are `forbid:`-protected

The whole point is that the agent conforms the *article* to reality. Letting
it "fix" the failure by adding a fabricated `S3` entry to `sources.json`, or
by editing the checker, would fake a green gate — exactly the "agent talks
its way past the verifier" failure bounded-loops exists to prevent. The
engine refuses any write to `seed/check_claims.py` or `seed/sources.json`.

## Make it real

Swap the stub runner for a real agent and point `sources.json` at (or have
the checker query) your actual citation/reference manager. Keep the gate as
the bottleneck: an article is never "done" until every claim traces to a
real, verifiable source.

## Which Anthropic pattern

`evaluator-optimizer` — the gate (`check_claims.py`) is the evaluator; the
agent-turn is the optimizer. The loop runs until the evaluator says every
citation is real.
Reference: [Anthropic — Building effective agents](https://www.anthropic.com/research/building-effective-agents)
