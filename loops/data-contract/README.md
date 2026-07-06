# data-contract

**Pattern:** evaluator-optimizer · **Role:** backend, api · **Rung:** L2 · **Gate:** jsonschema

Demonstrates the `JsonSchemaGate`: drive an agent until a JSON API
response (`output.json`) validates cleanly against a JSON Schema contract
(`schema.json`), using the real [`jsonschema`](https://github.com/python-jsonschema/jsonschema)
PyPI package.

## What happens

`seed/output.json` ships broken against `schema.json`'s user-record
contract: `created_at` is missing entirely, and `role` is set to
`"superuser"`, which is not in the schema's `admin`/`member`/`viewer`
enum. The loop runs an agent against `PROMPT.md`, checks `output.json`
against `schema.json` via `JsonSchemaGate` after each lap, and halts as
soon as the gate is clean.

## Prerequisites (one-time)

```bash
# jsonschema — install once, not part of the timed run below.
pip install jsonschema
```

## Prove the gate genuinely fails on the unfixed seed

```bash
cd loops/data-contract
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
jsonschema.exceptions.ValidationError: 'created_at' is a required property

Failed validating 'required' in schema:
    {'$schema': 'http://json-schema.org/draft-07/schema#',
     'title': 'UserRecord',
     ...
     'required': ['id', 'email', 'created_at', 'role'],
     'additionalProperties': False}

On instance:
    {'id': 42, 'email': 'jane.doe@example.com', 'role': 'superuser'}
```

The exact same broken instance also fails the `role` enum constraint
(`"superuser"` is not `admin`/`member`/`viewer`) — `jsonschema.validate`
reports the first violation it hits (`required` here) and stops; both
defects are present in `seed/output.json`.

## Run it with the engine

```bash
# from repo root
pip install -e .
bl lint loops/data-contract
bl run loops/data-contract --yes
```

Real captured output:

```
$ bl lint loops/data-contract
[PASS] /Users/varunpratapbhardwaj/Documents/work/varun-world/Agentic_official/bounded-loops/loops/data-contract

$ bl run loops/data-contract --yes
[bounded-loops] About to run loop 'data-contract':
  runner : stub
  gate   : <jsonschema gate>
✓ [DONE] gate-passed (laps: 1)  ledger: /Users/varunpratapbhardwaj/Documents/work/varun-world/Agentic_official/bounded-loops/loops/data-contract/.ledger.jsonl
```

Lap 1's cassette adds the missing `created_at` timestamp and corrects
`role` from `"superuser"` to the valid enum value `"admin"` —
`JsonSchemaGate` then validates `output.json` cleanly against
`schema.json`, and the loop reaches DONE.

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
