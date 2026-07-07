# roadmap-field-contract

**Pattern:** evaluator-optimizer · **Role:** business · **Rung:** L1 · **Gate:** jsonschema

Demonstrates `JsonSchemaGate`: drive an agent until a product
roadmap item (`output.json`) validates cleanly against a JSON Schema
contract (`schema.json`), using the real
[`jsonschema`](https://github.com/python-jsonschema/jsonschema) PyPI
package.

## What happens

`seed/output.json` ships broken against `schema.json`'s roadmap-item contract:
the required `owner` field is missing entirely, and `quarter` is set to
`"Q5"`, which is not in the schema's `Q1`/`Q2`/`Q3`/`Q4` enum. The loop runs
an agent against `PROMPT.md`, checks `output.json` against `schema.json` via
`JsonSchemaGate` after each lap, and halts as soon as the gate is clean.

## Prerequisites (one-time)

```bash
# jsonschema — install once, not part of the timed run below.
pip install jsonschema
```

## Prove the gate genuinely fails on the unfixed seed

```bash
cd loops/roadmap-field-contract
python3 -c "import jsonschema, json; jsonschema.validate(json.load(open('seed/output.json')), json.load(open('schema.json')))"
```

Real captured output (genuinely raised, not invented):

```
Traceback (most recent call last):
  ...
jsonschema.exceptions.ValidationError: 'owner' is a required property

Failed validating 'required' in schema:
    {'$schema': 'http://json-schema.org/draft-07/schema#',
     'title': 'RoadmapItem',
     ...
     'required': ['title', 'owner', 'quarter', 'status'],
     'additionalProperties': False}

On instance:
    {'title': 'Ship Loop Engineering course checkout', 'quarter': 'Q5', 'status': 'in_progress'}
```

The exact same broken instance also fails the `quarter` enum constraint
(`"Q5"` is not `Q1`/`Q2`/`Q3`/`Q4`) — `jsonschema.validate` reports the
first violation it hits (`required` here) and stops; both defects are
present in `seed/output.json`.

## Run it with the engine

```bash
# from repo root
pip install -e .
bl lint loops/roadmap-field-contract
bl run loops/roadmap-field-contract --yes
```

Expected output:

```
$ bl lint loops/roadmap-field-contract
[PASS] .../loops/roadmap-field-contract

$ bl run loops/roadmap-field-contract --yes
[bounded-loops] About to run loop 'roadmap-field-contract':
  runner : stub
  gate   : <jsonschema gate>
✓ [DONE] gate-passed (laps: 1)  ledger: .../loops/roadmap-field-contract/.ledger.jsonl
```

Lap 1's cassette adds the missing `owner` field and corrects `quarter` from
`"Q5"` to a valid enum value `"Q3"` — `JsonSchemaGate` then validates
`output.json` cleanly against `schema.json`, and the loop reaches DONE.

## Lift it into your own repo

1. Copy this folder.
2. Replace `schema.json` with your own contract, and `seed/output.json` with a
   real (or deliberately broken, for demo purposes) instance of it.
3. Edit `PROMPT.md` to describe your goal.
4. Run `bl run loops/<your-copy> --yes` to prove it works.

## Which Anthropic pattern

`evaluator-optimizer` — the gate (`JsonSchemaGate`) is the evaluator; the
agent-turn is the optimizer. The loop runs until the evaluator says the
contract is satisfied.
Reference: [Anthropic — Building effective agents](https://www.anthropic.com/research/building-effective-agents)
