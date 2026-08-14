# Reference graphs

Each directory here is one portable graph that **composes loop packages from [`loops/`](../loops/)**.
Nothing here is a new example: every node's gate is a real mechanical check that shipped and was
validated before the graph engine could run it.

Every `loop_package` is a **content digest** of a shipped package, so a graph names exact bytes rather
than a name that could drift. Editing a loop package invalidates every graph that pins it —
`tests/graphs/test_reference_graphs.py` fails on that drift and names the fix:

```bash
uv run python scripts/regenerate_reference_graphs.py
```

The shapes live in [`bounded_loops/graph/reference_graphs.py`](../bounded_loops/graph/reference_graphs.py)
so the generator and the test read one definition rather than two that agree by luck.

## What ships

Six graphs, one per domain, composing **24 distinct shipped loop packages**. Every one has the same
skeleton — three parallel checks → join → approval → one irreversible effect, plus a conditional
`when: failed` branch to a remediation loop — because that skeleton is what exercises the engine:
fan-out, cross-node causality, the guard grammar, a human checkpoint, and exactly one effect.

| Graph | Domain | Approval | Effect |
|---|---|---|---|
[`finance-payment-assurance`](finance-payment-assurance/) | finance | finance-controller | emit an ISO 20022 payment instruction |
[`retail-listing-release`](retail-listing-release/) | retail | merchandising-lead | release the listing to the storefront feed |
[`marketing-campaign-release`](marketing-campaign-release/) | marketing | content-editor | publish the campaign page |
[`engineering-release-gate`](engineering-release-gate/) | IT / development | release-manager | cut the release tag |
[`customer-data-request`](customer-data-request/) | customer | privacy-officer | send the data-subject response |
[`solo-builder-ship`](solo-builder-ship/) | personal projects | maintainer | ship the release notes |

No shipped loop carries a `customer`, `personal-projects` or `marketing` `role:` tag. Those three
graphs are assembled from `legal`, `operations` and `engineering` packages — see the reasoning below
on why the domain belongs to the graph.

## Running a graph end to end

All six reference graphs run end to end, keyless, with no spend — **from a checkout**.
Every loop node pins its package by content digest, and the `loops/` catalog ships in the
repository, not in the wheel, so a `pip install` alone leaves nothing for those digests to
resolve against. `git clone` first, or pass `--loop-roots` pointing at your own catalog.
`tests/graphs/test_reference_graphs.py` runs all six on every CI push.

```bash
bl graph run graphs/finance-payment-assurance/graph.yaml --execute --out /tmp/bl-run
# PAUSED — awaiting human decision on: approve-finance

bl graph approve --run /tmp/bl-run --node approve-finance --decision approved
# run_state: SUCCEEDED
```

The full skeleton exercises: three parallel `kind: loop` checks → `kind: join` (causality gate) →
`kind: approval` (HITL checkpoint) → `kind: publish` (single-fire effect ledger — one fire per
`run_id/plan_id/node_id`, including across repair rounds; the local ledger has no
compare-and-swap, so that holds for the sequential controller this engine ships).

You can also lint and plan without running:

```bash
bl graph lint graphs/finance-payment-assurance/graph.yaml    # validates the DAG
bl graph plan graphs/finance-payment-assurance/graph.yaml    # compiles to an execution plan
```

## Why the domain is a property of the graph, not the loop

A loop's `role:` tag is a **capability** ("this checks a ledger"), not a market segment. A workflow is
what belongs to a domain. So a domain graph is assembled from loops whose tags may differ from the
domain's name.

That reasoning is only honest under one condition, and it is worth stating because it is easy to
abuse: **the graph's irreversible effect must genuinely be that domain's effect, and every loop on the
critical path must be a check that effect requires.** Finance qualifies — you would not emit a payment
instruction without a three-way match, a balanced journal and a sane FX rate. A graph that cannot name
a mechanical check its publish step actually needs is a label on other people's gates, and it does not
belong here.
