# reproducibility-manifest

**Pattern:** evaluator-optimizer · **Role:** research · **Rung:** L1 · **Gate:** jsonschema

Demonstrates `JsonSchemaGate`: drive an agent until a research run's
reproducibility manifest (`output.json`) validates cleanly against a JSON
Schema contract (`schema.json`), using the real
[`jsonschema`](https://github.com/python-jsonschema/jsonschema) PyPI package.

## What happens

`seed/output.json` ships broken against `schema.json`'s reproducibility
contract: `data_hash` is missing entirely, and `seed` is given as the string
`"42"` instead of the required integer `42`. The loop runs an agent against
`PROMPT.md`, checks `output.json` against `schema.json` via `JsonSchemaGate`
after each lap, and halts as soon as the gate is clean.

## Prerequisites (one-time)

```bash
# jsonschema — install once, not part of the timed run below.
pip install jsonschema
```

## Prove the gate genuinely fails on the unfixed seed

```bash
cd loops/reproducibility-manifest
python3 -c "import jsonschema, json; jsonschema.validate(json.load(open('seed/output.json')), json.load(open('schema.json')))"
```

Real captured output (genuinely raised, not invented):

```
Traceback (most recent call last):
  File "<string>", line 1, in <module>
    import jsonschema, json; jsonschema.validate(json.load(open('seed/output.json')), json.load(open('schema.json')))
                             ~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File ".../site-packages/jsonschema/validators.py", line 1332, in validate
    raise error
jsonschema.exceptions.ValidationError: '42' is not of type 'integer'

Failed validating 'type' in schema['properties']['seed']:
    {'type': 'integer', 'description': 'Random seed used for the run.'}

On instance['seed']:
    '42'
```

The exact same broken instance also fails the `required` constraint
(`data_hash` is missing) — `jsonschema.validate` reports the first
violation it hits and stops; both defects are present in
`seed/output.json`.

## Run it with the engine

```bash
# from repo root
pip install -e .
bl lint loops/reproducibility-manifest
bl run loops/reproducibility-manifest --yes
```

Real captured output:

```
$ bl lint loops/reproducibility-manifest
[PASS] /Users/varunpratapbhardwaj/Documents/work/varun-world/Agentic_official/bounded-loops/loops/reproducibility-manifest

$ bl run loops/reproducibility-manifest --yes
[bounded-loops] About to run loop 'reproducibility-manifest':
  runner : stub
  gate   : <jsonschema gate>
✓ [DONE] gate-passed (laps: 1)  ledger: /Users/varunpratapbhardwaj/Documents/work/varun-world/Agentic_official/bounded-loops/loops/reproducibility-manifest/.ledger.jsonl
```

Lap 1's cassette adds the missing `data_hash` (a real-format 64-char hex
digest) and corrects `seed` from the string `"42"` to the integer `42` —
`JsonSchemaGate` then validates `output.json` cleanly against `schema.json`,
and the loop reaches DONE.

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
