# transport-request-manifest

**Role:** enterprise · erp · **Rung:** L2 · **Gate:** `command` (keyless) · **Runner:** stub (keyless)

A bounded loop that drives an agent until a **SAP transport request
manifest** has no dangling dependencies. In SAP's change and transport
system (CTS), a transport request carries a list of repository objects
between systems (development → quality → production); if it declares a
dependency on an object it does not itself carry (and that object hasn't
already landed via an earlier request), the import fails or lands in an
inconsistent state.

## What it demonstrates

The seed `transport.json` ships a request carrying a program, a table, and
a function group, but declares a dependency on a data element
(`R3TR DTEL ZINVOICE_STATUS`) that is not in its `objects` list — a
dangling dependency the receiving system cannot resolve.

The gate `seed/check_transport.py` verifies every id in `dependencies` also
appears in `objects`. The loop is DONE only when the checker exits 0.

## Run it (keyless, ~1s)

```bash
bl run loops/transport-request-manifest --yes   # stub runner + real command gate
```

You'll see the ungated manifest fail the checker (dangling dependency), the
recorded fix add the missing object, then the gate pass.

## Why the checker is `forbid:`-protected

The whole point is that the agent conforms the *manifest* to reality.
Letting it "fix" the failure by deleting the dependency instead of carrying
the object, or by editing the checker, would fake a green gate — exactly
the "agent talks its way past the verifier" failure bounded-loops exists to
prevent. The engine refuses any write to `seed/check_transport.py`.

## Make it real

Swap the stub runner for a real agent and point the gate at a real
transport-object query — `RSTMS`/`SE01` object list export, or a CTS API
call. Keep the gate as the bottleneck: a transport request is never "done"
until every dependency it declares actually travels with it.
