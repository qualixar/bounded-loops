# agent-authoring

**Pattern:** evaluator-optimizer · **Role:** agent-development, tooling · **Rung:** L2 · **Gate:** jsonschema

Demonstrates the `JsonSchemaGate`: drive an agent until a freshly
authored sub-agent config (`output.json`) conforms to a required JSON
Schema (`schema.json`) before it is allowed to be registered.

## What happens

`seed/output.json` ships as a BROKEN sub-agent config: `tools` is missing
entirely, and `model` is set to `"gpt-4"`, which is not one of the allowed
values (`sonnet`/`opus`/`haiku`). `name` and `description` already
conform. The loop runs an agent against `PROMPT.md`, checks `output.json`
against `schema.json` after each lap via `JsonSchemaGate`, and halts as
soon as the gate is clean. The agent is explicitly told not to loosen
`schema.json` to make the broken config pass — only the instance may
change.

## Prerequisites (one-time)

```bash
# jsonschema — installed automatically as a project dependency
# (bounded-loops' pyproject.toml lists it; no separate install needed
# once `pip install -e .` has been run from the repo root).
```

## Prove the gate genuinely fails on the unfixed seed

```bash
cd loops/agent-authoring
python3 -c "import jsonschema, json; jsonschema.validate(json.load(open('seed/output.json')), json.load(open('schema.json')))"
```

Real captured output:
```
Traceback (most recent call last):
  File "<string>", line 1, in <module>
  File ".../jsonschema/validators.py", line 1332, in validate
    raise error
jsonschema.exceptions.ValidationError: 'tools' is a required property

Failed validating 'required' in schema:
    {'$schema': 'http://json-schema.org/draft-07/schema#',
     'title': 'Sub-agent configuration',
     'description': 'Required shape for an authored sub-agent config '
                    'before it may be registered.',
     'type': 'object',
     'properties': {'name': {'type': 'string',
                             'pattern': '^[a-z][a-z0-9-]*$'},
                    'description': {'type': 'string', 'minLength': 20},
                    'tools': {'type': 'array',
                              'minItems': 1,
                              'items': {'type': 'string',
                                        'enum': ['Read',
                                                 'Write',
                                                 'Edit',
                                                 'Bash',
                                                 'Grep',
                                                 'Glob']}},
                    'model': {'type': 'string',
                              'enum': ['sonnet', 'opus', 'haiku']}},
     'required': ['name', 'description', 'tools', 'model'],
     'additionalProperties': False}

On instance:
    {'name': 'release-notes-writer',
     'description': 'Drafts release notes from a merged PR list and '
                    'changelog entries.',
     'model': 'gpt-4'}
```

`tools` is reported first because `jsonschema.validate` raises on the
first violation it encounters (both the missing `tools` and the invalid
`model` are real violations of this schema — `JsonSchemaGate.check()`
reports whichever `ValidationError.message` the underlying library
surfaces, matching this exactly).

A corrected instance — the same content the engine's cassette writes on
lap 1 — was independently confirmed to validate cleanly against the same
schema with zero exception raised (throwaway check run from `/tmp`,
loading `schema.json` from this loop and validating
`{"name": "release-notes-writer", "description": "Drafts release notes
from a merged PR list and changelog entries.", "tools": ["Read", "Grep"],
"model": "sonnet"}` against it): `jsonschema.validate` returned normally.

## Run it with the engine

```bash
# from repo root
pip install -e .
bl lint loops/agent-authoring
bl run loops/agent-authoring --yes
```

Real captured output:
```
[PASS] /path/to/bounded-loops/loops/agent-authoring
```
```
[bounded-loops] About to run loop 'agent-authoring':
  runner : stub
  gate   : <jsonschema gate>
✓ [DONE] gate-passed (laps: 1)  ledger: /path/to/bounded-loops/loops/agent-authoring/.ledger.jsonl
```

Lap 1's cassette adds a valid, non-empty `tools` array (`["Read",
"Grep"]`) and sets `model` to an allowed value (`"sonnet"`) —
`JsonSchemaGate` then reports `output.json validates against schema`
(`Verdict(passed=True)`), and the loop reaches DONE. The real ledger entry
recorded for this run:
```
{"lap":1,"ts":"2026-07-06T00:27:58.395726Z","verdict":{"passed":true,"detail":"output.json validates against schema","evidence":{}},"decision":"done","budget_spent":{"laps":1,"tokens":231,"wallclock_s":0.02}}
```

## Lift it into your own repo

1. Copy this folder.
2. Replace `schema.json` with the required shape for your own artifact,
   and `seed/output.json` with a deliberately-broken starting instance.
3. Edit `PROMPT.md` to describe your goal — and keep the "fix the
   instance, not the schema" instruction; it is the whole point of this
   pattern.
4. Run `bl run loops/<your-copy> --yes` to prove it works.

## Which Anthropic pattern

`evaluator-optimizer` — the gate (JSON Schema validation) is the
evaluator; the agent-turn is the optimizer. The loop runs until the
evaluator says the instance conforms.
Reference: [Anthropic — Building effective agents](https://www.anthropic.com/research/building-effective-agents)
