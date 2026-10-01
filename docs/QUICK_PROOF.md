# A keyless acceptance-gate proof

Install the published package and copy the demo into an explicit writable directory:

```bash
python -m pip install bounded-loops
bl loops install bug-fix-red-green --dest ./loops
bash loops/bug-fix-red-green/wreck.sh
# Expected exit 1: the ungated stub claims GREEN while its tests fail.
bl run loops/bug-fix-red-green --yes --run-id first-proof
```

The worker is a synthetic stub; the pytest gate is real. The local verification
on 1 October 2026 passed six tests and reached DONE in one lap. No hosted model,
accuracy benchmark, latency comparison or savings estimate is involved.
Use a fresh run-id for each new run. Inspect the gate and runner before using --yes.

Record the printed ledger_head independently of the run directory, then verify:

```bash
bl verify loops/bug-fix-red-green/.bounded-loops/runs/first-proof/ledger.jsonl --expect-head YOUR_RECORDED_HEAD --json
```

The local persisted proof passed chain, separate-head match and completeness.
A plain chain check without a retained head and run metadata is weaker evidence;
it should not be described as all checks verified. The external head is useful
only if the party editing the run directory cannot also replace your retained copy.

The CLI may print its trust notice before JSON output. Consumers must distinguish
that notice from the outcome object rather than assuming all stdout is one JSON value.
The [paper](https://arxiv.org/abs/2609.27871) states the research protocol and assumptions.
