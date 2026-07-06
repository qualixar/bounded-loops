# clinical-note-completeness

**Pattern:** evaluator-optimizer · **Role:** healthcare, clinical-documentation · **Rung:** L3 · **Gate:** jsonschema

Demonstrates the `JsonSchemaGate` in a HEALTHCARE setting: drive an
agent until a SOAP clinical note (`output.json`) validates cleanly
against a JSON Schema contract (`schema.json`), using the real
[`jsonschema`](https://github.com/python-jsonschema/jsonschema) PyPI
package. This is the same gate as `loops/data-contract/`, applied to a
different industry — proof that bounded-loops spans industries, not
just software.

## What happens

`seed/output.json` ships a broken SOAP note against `schema.json`'s
contract: the `plan` section (treatment plan and next steps) is
missing entirely — a common incomplete-note error — and `icd10_codes`
contains `"ABC"`, which is not shaped like a real ICD-10-CM code. The
loop runs an agent against `PROMPT.md`, checks `output.json` against
`schema.json` via `JsonSchemaGate` after each lap, and halts as soon as
the gate is clean.

## Prerequisites (one-time)

```bash
# jsonschema — install once, not part of the timed run below.
pip install jsonschema
```

## Prove the gate genuinely fails on the unfixed seed

```bash
cd loops/clinical-note-completeness
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
jsonschema.exceptions.ValidationError: 'plan' is a required property

Failed validating 'required' in schema:
    {'$schema': 'http://json-schema.org/draft-07/schema#',
     'title': 'SOAPClinicalNote',
     ...
     'required': ['patient_id', 'encounter_date', 'subjective',
                  'objective', 'assessment', 'plan', 'icd10_codes'],
     'additionalProperties': False}

On instance:
    {'patient_id': 'PT-10492',
     'encounter_date': '2026-07-06',
     'subjective': 'Patient reports increased thirst, frequent urination, ...',
     'objective': 'Vitals: BP 138/86, HR 82, BMI 31.2. Random plasma glucose ...',
     'assessment': 'New-onset type 2 diabetes mellitus, likely contributed ...',
     'icd10_codes': ['ABC']}
```

The exact same broken instance also fails the `icd10_codes` pattern
constraint (`"ABC"` doesn't match the ICD-10-CM shape) —
`jsonschema.validate` reports the first violation it hits (`required`
here, for the missing `plan` field) and stops; both defects are
present in `seed/output.json`.

## Run it with the engine

```bash
# from repo root
pip install -e .
bl lint loops/clinical-note-completeness
bl run loops/clinical-note-completeness --yes
```

Real captured output:

```
$ bl lint loops/clinical-note-completeness
[PASS] /Users/varunpratapbhardwaj/Documents/work/varun-world/Agentic_official/bounded-loops/loops/clinical-note-completeness

$ bl run loops/clinical-note-completeness --yes
[bounded-loops] About to run loop 'clinical-note-completeness':
  runner : stub
  gate   : <jsonschema gate>
✓ [DONE] gate-passed (laps: 1)  ledger: /Users/varunpratapbhardwaj/Documents/work/varun-world/Agentic_official/bounded-loops/loops/clinical-note-completeness/.ledger.jsonl
```

Lap 1's cassette adds the missing `plan` section (a treatment plan
consistent with the `assessment`) and corrects `icd10_codes` from the
invalid `"ABC"` to real, correctly-shaped ICD-10-CM codes (`E11.9` for
new-onset type 2 diabetes, `I10` for the noted elevated blood
pressure) — `JsonSchemaGate` then validates `output.json` cleanly
against `schema.json`, and the loop reaches DONE.

## Important: what this gate does NOT check

This loop's gate checks **structural completeness** (are all seven
SOAP-note fields present and non-empty) and **ICD-10 code shape** (does
each code match the `^[A-TV-Z][0-9][0-9AB](\.[0-9A-TV-Z]{1,4})?$`
pattern real ICD-10-CM codes follow). It does **not** check clinical
correctness — it cannot tell whether the diagnosis is medically
accurate, whether the plan is appropriate for the assessment, or
whether the chosen ICD-10 code is the *right* code for the patient's
actual condition. A real deployment of this loop in a clinical setting
must gate on human sign-off: this is exactly why `bounds.yaml` sets the
loop's rung to L3 and carries a `require_approval` flag — in production
that flag should be `true` so a licensed clinician reviews and approves
the note before it is accepted, with the schema gate only pre-screening
for completeness so the clinician isn't reviewing obviously-incomplete
drafts. This demo sets `require_approval: false` solely so it can run
keyless and non-interactively; flip it back to `true` before using this
pattern on real patient data.

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
