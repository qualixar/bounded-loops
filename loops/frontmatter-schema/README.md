# frontmatter-schema

**Pattern:** evaluator-optimizer · **Role:** content · **Rung:** L1 · **Gate:** jsonschema

Demonstrates `JsonSchemaGate`: drive an agent until a blog post's parsed
frontmatter (`output.json`) validates cleanly against a JSON Schema contract
(`schema.json`), using the real
[`jsonschema`](https://github.com/python-jsonschema/jsonschema) PyPI package.

## What happens

`seed/output.json` ships broken against `schema.json`'s frontmatter contract:
`slug` is missing entirely, and `tags` is set to a bare string
(`"ai-reliability"`) instead of an array of strings. The loop runs an agent
against `PROMPT.md`, checks `output.json` against `schema.json` via
`JsonSchemaGate` after each lap, and halts as soon as the gate is clean.

## Prerequisites (one-time)

```bash
# jsonschema — install once, not part of the timed run below.
pip install jsonschema
```

## Prove the gate genuinely fails on the unfixed seed

```bash
cd loops/frontmatter-schema
python3 -c "import jsonschema, json; jsonschema.validate(json.load(open('seed/output.json')), json.load(open('schema.json')))"
```

The broken instance fails validation: `slug` is a required property that's
missing, and (independently) `tags` is a string where the schema requires an
array.

## Run it with the engine

```bash
# from repo root
pip install -e .
bl lint loops/frontmatter-schema
bl run loops/frontmatter-schema --yes
```

Lap 1's cassette adds a valid `slug` and converts `tags` from a bare string
into a one-item array — `JsonSchemaGate` then validates `output.json` cleanly
against `schema.json`, and the loop reaches DONE.

## Lift it into your own repo

1. Copy this folder.
2. Replace `schema.json` with your own contract, and `seed/output.json` with
   a real (or deliberately broken, for demo purposes) instance of it.
3. Edit `PROMPT.md` to describe your goal.
4. Run `bl run loops/<your-copy> --yes` to prove it works.

## Which Anthropic pattern

`evaluator-optimizer` — the gate (`JsonSchemaGate`) is the evaluator; the
agent-turn is the optimizer. The loop runs until the evaluator says the
contract is satisfied.
Reference: [Anthropic — Building effective agents](https://www.anthropic.com/research/building-effective-agents)
