# contract-clause-extraction

**Pattern:** evaluator-optimizer · **Role:** legal, compliance · **Rung:** L2 · **Gate:** jsonschema

Demonstrates the `JsonSchemaGate` in a **legal-industry** setting: drive
an agent until an extracted-clauses record (`output.json`) validates cleanly
against a required-clauses JSON Schema contract (`schema.json`), using the
real [`jsonschema`](https://github.com/python-jsonschema/jsonschema) PyPI
package. This is the same gate as `loops/data-contract`, applied to a
different domain — proving bounded-loops spans industries, not just
software.

## What happens

`seed/output.json` ships a broken clause extraction from a (fictional)
Master Services Agreement between Acme Corp and Globex LLC. It is broken
against `schema.json`'s required-clauses contract in two realistic ways:
the `confidentiality` clause is missing entirely from `clauses`, and
`governing_law` is a bare string (`"Delaware"`) instead of the required
`{present, text}` object. The loop runs an agent against `PROMPT.md`,
checks `output.json` against `schema.json` via `JsonSchemaGate` after each
lap, and halts as soon as the gate is clean.

## Prerequisites (one-time)

```bash
# jsonschema — install once, not part of the timed run below.
pip install jsonschema
```

## Prove the gate genuinely fails on the unfixed seed

```bash
cd loops/contract-clause-extraction
python3 -c "import jsonschema, json; jsonschema.validate(json.load(open('seed/output.json')), json.load(open('schema.json')))"
```

Real captured output (genuinely raised, not invented):

```
Traceback (most recent call last):
  File "<string>", line 1, in <module>
  File ".../site-packages/jsonschema/validators.py", line 1332, in validate
    raise error
jsonschema.exceptions.ValidationError: 'confidentiality' is a required property

Failed validating 'required' in schema['properties']['clauses']:
    {'type': 'object',
     'description': 'Extraction result for each required clause type.',
     'properties': {'termination': {'$ref': '#/definitions/clause'},
                    'indemnification': {'$ref': '#/definitions/clause'},
                    'governing_law': {'$ref': '#/definitions/clause'},
                    'confidentiality': {'$ref': '#/definitions/clause'}},
     'required': ['termination',
                  'indemnification',
                  'governing_law',
                  'confidentiality'],
     'additionalProperties': False}

On instance['clauses']:
    {'termination': {'present': True,
                     'text': 'Either party may terminate this Agreement '
                             'upon 30 days written notice.'},
     'indemnification': {'present': True,
                         'text': 'Each party shall indemnify the other '
                                 'against third-party claims arising from '
                                 'breach of this Agreement.'},
     'governing_law': 'Delaware'}
```

The exact same broken instance also fails the `governing_law` shape
constraint (a bare string instead of the required `{present, text}`
object) — `jsonschema.validate` reports the first violation it hits
(the missing `confidentiality` key) and stops; both defects are present
in `seed/output.json`.

## Run it with the engine

```bash
# from repo root
pip install -e .
bl lint loops/contract-clause-extraction
bl run loops/contract-clause-extraction --yes
```

Real captured output:

```
$ bl lint loops/contract-clause-extraction
[PASS] /Users/varunpratapbhardwaj/Documents/work/varun-world/Agentic_official/bounded-loops/loops/contract-clause-extraction

$ bl run loops/contract-clause-extraction --yes
[bounded-loops] About to run loop 'contract-clause-extraction':
  runner : stub
  gate   : <jsonschema gate>
✓ [DONE] gate-passed (laps: 1)  ledger: /Users/varunpratapbhardwaj/Documents/work/varun-world/Agentic_official/bounded-loops/loops/contract-clause-extraction/.ledger.jsonl
```

Lap 1's cassette adds the missing `confidentiality` clause object and
converts `governing_law` from a bare string to the required `{present,
text}` shape — `JsonSchemaGate` then validates `output.json` cleanly
against `schema.json`, and the loop reaches DONE.

## Lift it into your own repo

1. Copy this folder.
2. Replace `schema.json` with your own contract (e.g. a different set of
   required clauses, or an entirely different document type), and
   `seed/output.json` with a real (or deliberately broken, for demo
   purposes) instance of it.
3. Edit `PROMPT.md` to describe your goal.
4. Run `bl run loops/<your-copy> --yes` to prove it works.

## Which Anthropic pattern

`evaluator-optimizer` — the gate (`JsonSchemaGate`) is the evaluator; the
agent-turn is the optimizer. The loop runs until the evaluator says the
contract is satisfied.
Reference: [Anthropic — Building effective agents](https://www.anthropic.com/research/building-effective-agents)
