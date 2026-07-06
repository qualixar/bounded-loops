# catalog-required-fields

**Pattern:** evaluator-optimizer · **Role:** retail · **Rung:** L1 · **Gate:** jsonschema

Demonstrates `JsonSchemaGate`: drive an agent until a retail catalog
entry (`output.json`) carries all of its required display fields per
`schema.json`, using the real
[`jsonschema`](https://github.com/python-jsonschema/jsonschema) PyPI package.

## What happens

`output.json` ships broken against `schema.json`'s required-fields contract:
`description` is missing entirely. The loop runs an agent against
`PROMPT.md`, checks `output.json` against `schema.json` via `JsonSchemaGate`
after each lap, and halts as soon as the gate is clean.

## Prerequisites (one-time)

```bash
# jsonschema — install once, not part of the timed run below.
pip install jsonschema
```

## Prove the gate genuinely fails on the unfixed seed

```bash
cd loops/catalog-required-fields
python3 -c "import jsonschema, json; jsonschema.validate(json.load(open('output.json')), json.load(open('schema.json')))"
```

The unfixed instance is missing `description`, a required property, so
validation fails.

## Run it with the engine

```bash
# from repo root
pip install -e .
bl lint loops/catalog-required-fields
bl run loops/catalog-required-fields --yes
```

Lap 1's cassette adds a valid `description` string — `JsonSchemaGate` then
validates `output.json` cleanly against `schema.json`, and the loop reaches
DONE.

## Lift it into your own repo

1. Copy this folder.
2. Replace `schema.json` with your own contract, and `output.json` with a
   real (or deliberately broken, for demo purposes) instance of it.
3. Edit `PROMPT.md` to describe your goal.
4. Run `bl run loops/<your-copy> --yes` to prove it works.

## Which Anthropic pattern

`evaluator-optimizer` — the gate (`JsonSchemaGate`) is the evaluator; the
agent-turn is the optimizer. The loop runs until the evaluator says the
contract is satisfied.
Reference: [Anthropic — Building effective agents](https://www.anthropic.com/research/building-effective-agents)
