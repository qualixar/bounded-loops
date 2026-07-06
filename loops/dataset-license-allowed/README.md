# dataset-license-allowed

**Role:** research · **Rung:** L1 · **Gate:** `command` (keyless) · **Runner:** stub (keyless)

A bounded loop that drives an agent until **every dataset in a training
manifest carries a license that is cleared by the project's allowlist**. This
is the runnable form of a common ML-compliance failure: a disallowed-license
dataset (often copyleft, like GPL-3.0) getting mixed into a permissively
licensed training set.

## What it demonstrates

The seed `datasets.json` lists four datasets. One is wrong:

- **`SynthConvQA` — `GPL-3.0`** — a real copyleft license, but not in
  `allowlist.json` (`MIT`, `Apache-2.0`, `CC-BY-4.0`). It is not a mislabel of
  an allowed license, so the correct fix is to drop the dataset, not relabel
  its license.

The gate `seed/check_licenses.py` derives the set of allowed license ids from
`allowlist.json` and flags any dataset entry whose license is not in that
set. The loop is DONE only when the checker exits 0.

## Run it (keyless, ~1s)

```bash
bl run loops/dataset-license-allowed --yes   # stub runner + real command gate
```

You'll see the ungated manifest fail the checker, the recorded fix drop the
disallowed-license dataset, then the gate pass.

## Why the allowlist and checker are `forbid:`-protected

The whole point is that the agent conforms the *manifest* to the license
policy. Letting it "fix" the failure by adding `GPL-3.0` to
`allowlist.json`, or by relabeling `SynthConvQA`'s license to something
allowed, or by editing the checker, would fake a green gate — exactly the
"agent talks its way past the verifier" failure bounded-loops exists to
prevent. The engine refuses any write to `seed/check_licenses.py` or
`seed/allowlist.json`.

## Make it real

Swap the stub runner for a real agent and point `allowlist.json` at (or have
the checker query) your actual legal-approved license list. Keep the gate as
the bottleneck: a dataset manifest is never "done" until every license is
independently verified to be cleared for use.

## Which Anthropic pattern

`evaluator-optimizer` — the gate (`check_licenses.py`) is the evaluator; the
agent-turn is the optimizer. The loop runs until the evaluator says every
dataset's license is cleared.
Reference: [Anthropic — Building effective agents](https://www.anthropic.com/research/building-effective-agents)
