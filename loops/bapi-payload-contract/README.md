# bapi-payload-contract

**Role:** enterprise · erp · **Rung:** L2 · **Gate:** jsonschema (keyless) · **Runner:** stub (keyless)

Demonstrates `JsonSchemaGate` against a **BAPI_MATERIAL_SAVEDATA-style**
material-create payload: drive an agent until `output.json` validates
cleanly against `schema.json`'s contract, using the real
[`jsonschema`](https://github.com/python-jsonschema/jsonschema) PyPI
package.

## What happens

`seed/output.json` ships broken against `schema.json`'s material-payload
contract: `HEADDATA` is missing its required `MATL_TYPE` field (material
type, e.g. `FERT`/`ROH`) — a payload SAP's `BAPI_MATERIAL_SAVEDATA` would
reject outright, since material type governs which fields are mandatory
downstream. The loop runs an agent against `PROMPT.md`, checks
`output.json` against `schema.json` via `JsonSchemaGate` after each lap,
and halts as soon as the gate is clean.

## Prerequisites (one-time)

```bash
pip install jsonschema
```

## Prove the gate genuinely fails on the unfixed seed

```bash
cd loops/bapi-payload-contract
python3 -c "import jsonschema, json; jsonschema.validate(json.load(open('seed/output.json')), json.load(open('schema.json')))"
```

This raises `jsonschema.exceptions.ValidationError: 'MATL_TYPE' is a
required property` against the `HEADDATA` sub-schema.

## Run it with the engine

```bash
# from repo root
pip install -e .
bl lint loops/bapi-payload-contract
bl run loops/bapi-payload-contract --yes
```

Lap 1's cassette adds the missing `MATL_TYPE` value to `HEADDATA` —
`JsonSchemaGate` then validates `output.json` cleanly against
`schema.json`, and the loop reaches DONE.

## Lift it into your own repo

1. Copy this folder.
2. Replace `schema.json` with your own BAPI/RFC-style contract, and
   `seed/output.json` with a real (or deliberately broken, for demo
   purposes) instance of it.
3. Edit `PROMPT.md` to describe your goal.
4. Run `bl run loops/<your-copy> --yes` to prove it works.

## Which Anthropic pattern

`evaluator-optimizer` — the gate (`JsonSchemaGate`) is the evaluator; the
agent-turn is the optimizer. The loop runs until the evaluator says the
contract is satisfied.
Reference: [Anthropic — Building effective agents](https://www.anthropic.com/research/building-effective-agents)
