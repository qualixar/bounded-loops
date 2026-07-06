# json-config-schema

**Role:** backend · api · **Rung:** L1 · **Gate:** `jsonschema` (keyless) · **Runner:** stub (keyless)

A bounded loop that drives an agent until **a service's JSON runtime config
conforms to its contract**.

## What it demonstrates

The seed `output.json` claims to configure a `billing-api` service, but two
fields are broken: `port` is the string `"8080"` instead of an integer, and
`log_level` is missing entirely.

`schema.json` requires `service` (string), `port` (integer, 1..65535), and
`log_level` (string, enum `debug`/`info`/`warn`/`error`), with
`additionalProperties: false`. The loop is DONE only when
`jsonschema.validate` raises no error against this schema.

## Run it (keyless, ~1s)

```bash
bl run loops/json-config-schema --yes   # stub runner + real jsonschema gate
```

You'll see the broken config fail validation, the recorded fix convert
`port` to an integer and add a valid `log_level`, then the gate pass.

## Why the schema is the ground truth

The whole point is that the agent conforms the *data* to the contract.
Loosening `schema.json` — dropping `required` entries, widening `port`'s
range, or removing `additionalProperties: false` — to make the broken data
pass would fake a green gate — exactly the "agent talks its way past the
verifier" failure bounded-loops exists to prevent. The contract is the
ground truth, not the data.

## Make it real

Swap the stub runner for a real agent and point this schema at your actual
service's config contract. Keep the gate as the bottleneck: a config is never
"done" until it validates cleanly against the schema every deploy depends on.
