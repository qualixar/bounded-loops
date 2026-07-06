# openapi-schema-valid

**Role:** backend · api · **Rung:** L1 · **Gate:** `command` (keyless) · **Runner:** stub (keyless)

A bounded loop that drives an agent until **an OpenAPI 3 document satisfies
its structural minimum**: a top-level `openapi` version, `info.title` /
`info.version`, and every operation under `paths` declaring a non-empty
`responses` object.

## What it demonstrates

The seed `openapi.json` describes a small "Widgets API" with three
operations. Two of them (`GET /widgets`, `GET /widgets/{id}`) correctly
declare `responses`. The third, `POST /widgets`, has no `responses` object at
all — an undocumented contract with no defined outcome, which most OpenAPI
tooling (codegen, gateways, docs generators) silently mishandles or rejects.

The gate `seed/check_openapi.py` walks every operation under `paths` and
flags any missing/empty `responses`. The loop is DONE only when the checker
exits 0.

## Run it (keyless, ~1s)

```bash
bl run loops/openapi-schema-valid --yes   # stub runner + real command gate
```

You'll see the ungated document fail the checker, the recorded fix add a
`responses` block to `POST /widgets`, then the gate pass.

## Why the checker is `forbid:`-protected

The whole point is that the agent conforms the *document* to the contract.
Letting it "fix" the failure by deleting the offending operation, or by
editing the checker to stop checking `responses`, would fake a green gate —
exactly the "agent talks its way past the verifier" failure bounded-loops
exists to prevent. The engine refuses any write to `seed/check_openapi.py`.

## Make it real

Swap the stub runner for a real agent and swap the hand-rolled checker for a
real OpenAPI validator library (e.g. `openapi-spec-validator`) if you want
full spec coverage beyond this minimal structural check. Keep the gate as the
bottleneck: a spec is never "done" until every operation's contract is fully
defined.
