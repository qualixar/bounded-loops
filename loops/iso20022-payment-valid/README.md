# iso20022-payment-valid

**Pattern:** evaluator-optimizer · **Role:** finance · **Rung:** L2 · **Gate:** jsonschema

Demonstrates `JsonSchemaGate`: drive an agent until a simplified
ISO 20022 `pain.001` credit-transfer instruction (`output.json`) validates
cleanly against a JSON Schema contract (`schema.json`), using the real
[`jsonschema`](https://github.com/python-jsonschema/jsonschema) PyPI package.

## What happens

`seed/output.json` ships broken against `schema.json`'s credit-transfer
contract: `amount` is the string `"1500.00"` instead of a positive number,
and `currency` is missing entirely. The loop runs an agent against
`PROMPT.md`, checks `output.json` against `schema.json` via `JsonSchemaGate`
after each lap, and halts as soon as the gate is clean.

## Prerequisites (one-time)

```bash
# jsonschema — install once, not part of the timed run below.
pip install jsonschema
```

## Prove the gate genuinely fails on the unfixed seed

```bash
cd loops/iso20022-payment-valid
python3 -c "import jsonschema, json; jsonschema.validate(json.load(open('seed/output.json')), json.load(open('schema.json')))"
```

This raises `jsonschema.exceptions.ValidationError` — `amount` is not of
type `number` (it's a string), which is the first violation `validate`
reports; the missing `currency` field is a second, independent defect
present in the same broken instance.

## Run it with the engine

```bash
# from repo root
pip install -e .
bl lint loops/iso20022-payment-valid
bl run loops/iso20022-payment-valid --yes
```

Expected:

```
$ bl lint loops/iso20022-payment-valid
[PASS] .../loops/iso20022-payment-valid

$ bl run loops/iso20022-payment-valid --yes
[bounded-loops] About to run loop 'iso20022-payment-valid':
  runner : stub
  gate   : <jsonschema gate>
✓ [DONE] gate-passed (laps: 1)  ledger: .../loops/iso20022-payment-valid/.ledger.jsonl
```

Lap 1's cassette rewrites `output.json` with `amount` as the number
`1500.00` and adds `currency: "EUR"` — `JsonSchemaGate` then validates
cleanly against `schema.json`, and the loop reaches DONE.

## Lift it into your own repo

1. Copy this folder.
2. Replace `schema.json` with your own contract, and `seed/output.json`
   with a real (or deliberately broken, for demo purposes) instance of it.
3. Edit `PROMPT.md` to describe your goal.
4. Run `bl run loops/<your-copy> --yes` to prove it works.

## Which Anthropic pattern

`evaluator-optimizer` — the gate (`JsonSchemaGate`) is the evaluator; the
agent-turn is the optimizer. The loop runs until the evaluator says the
contract is satisfied.
Reference: [Anthropic — Building effective agents](https://www.anthropic.com/research/building-effective-agents)
