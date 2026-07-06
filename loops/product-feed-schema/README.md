# product-feed-schema

**Pattern:** evaluator-optimizer · **Role:** retail · **Rung:** L1 · **Gate:** jsonschema

Demonstrates `JsonSchemaGate`: drive an agent until a Google-Merchant-
style product feed item (`output.json`) validates cleanly against a JSON
Schema contract (`schema.json`), using the real
[`jsonschema`](https://github.com/python-jsonschema/jsonschema) PyPI package.

## What happens

`output.json` ships broken against `schema.json`'s product-feed contract:
`image_link` is missing entirely, and `price` is shipped as the string
`"14.99"` instead of the number `14.99`. The loop runs an agent against
`PROMPT.md`, checks `output.json` against `schema.json` via `JsonSchemaGate`
after each lap, and halts as soon as the gate is clean.

## Prerequisites (one-time)

```bash
# jsonschema — install once, not part of the timed run below.
pip install jsonschema
```

## Prove the gate genuinely fails on the unfixed seed

```bash
cd loops/product-feed-schema
python3 -c "import jsonschema, json; jsonschema.validate(json.load(open('output.json')), json.load(open('schema.json')))"
```

The unfixed instance is missing `image_link` (a required property) and has
`price` typed as a string, either of which independently fails validation.

## Run it with the engine

```bash
# from repo root
pip install -e .
bl lint loops/product-feed-schema
bl run loops/product-feed-schema --yes
```

Lap 1's cassette adds the missing `image_link` URI and corrects `price` from
the string `"14.99"` to the number `14.99` — `JsonSchemaGate` then validates
`output.json` cleanly against `schema.json`, and the loop reaches DONE.

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
