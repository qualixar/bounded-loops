# skill-authoring

**Pattern:** evaluator-optimizer · **Role:** agent-development, tooling · **Rung:** L2 · **Gate:** jsonschema

Demonstrates the `JsonSchemaGate`: drive an agent until an authored
agent-skill's metadata/frontmatter conforms to a required JSON Schema shape
before it is considered publishable.

## What happens

`seed/output.json` ships a broken skill manifest: `name` is `"My Cool
Skill"` (spaces + uppercase, violates the required kebab-case pattern
`^[a-z][a-z0-9-]*$`) and `description` is `"does stuff"` (10 characters,
under the required 20-character minimum). `allowed_tools` is already a
valid non-empty array, so the gate failure is isolated to exactly those two
fields. The loop runs an agent against `PROMPT.md`, checks `output.json`
against `schema.json` after each lap via `JsonSchemaGate`, and halts as
soon as the gate is clean.

## Prove the gate genuinely fails on the unfixed seed

```bash
cd loops/skill-authoring
python3 -c "
import json, jsonschema
schema = json.load(open('schema.json'))
instance = json.load(open('seed/output.json'))
jsonschema.validate(instance=instance, schema=schema)
"
```

Real captured output (this repo's `.venv`, `jsonschema` 4.26.0):
```
Traceback (most recent call last):
  File "<string>", line 5, in <module>
  File ".../jsonschema/validators.py", line 1332, in validate
    raise error
jsonschema.exceptions.ValidationError: 'My Cool Skill' does not match '^[a-z][a-z0-9-]*$'

Failed validating 'pattern' in schema['properties']['name']:
    {'type': 'string',
     'pattern': '^[a-z][a-z0-9-]*$',
     'description': "Kebab-case skill identifier, e.g. 'pdf-extractor'."}

On instance['name']:
    'My Cool Skill'
```

Enumerating every violation (not just the first) confirms exactly two,
matching the two intentionally-broken fields and nothing else:
```
Total errors found: 2
 - path=['description']: 'does stuff' is too short
 - path=['name']: 'My Cool Skill' does not match '^[a-z][a-z0-9-]*$'
```

## Run it with the engine

```bash
# from repo root
pip install -e .
bl lint loops/skill-authoring
bl run loops/skill-authoring --yes
```

Real captured output (repo-root-relative path trimmed for readability; the
CLI prints an absolute path):
```
[PASS] .../loops/skill-authoring
```
```
[bounded-loops] About to run loop 'skill-authoring':
  runner : stub
  gate   : <jsonschema gate>
✓ [DONE] gate-passed (laps: 1)  ledger: .../loops/skill-authoring/.ledger.jsonl
```

Lap 1's cassette rewrites `name` to `"my-cool-skill"` and `description` to
a substantive 80-character explanation — `JsonSchemaGate` then reports
`output.json validates against schema` (exit 0 equivalent: `passed: true`),
and the loop reaches DONE on lap 1.

## Lift it into your own repo

1. Copy this folder.
2. Replace `schema.json` with your own required shape and `seed/output.json`
   with a deliberately-broken instance of it.
3. Edit `PROMPT.md` to describe your goal.
4. Run `bl run loops/<your-copy> --yes` to prove it works.

## Which Anthropic pattern

`evaluator-optimizer` — the gate (`JsonSchemaGate`) is the evaluator; the
agent-turn is the optimizer. The loop runs until the evaluator says the
manifest validates.
Reference: [Anthropic — Building effective agents](https://www.anthropic.com/research/building-effective-agents)
