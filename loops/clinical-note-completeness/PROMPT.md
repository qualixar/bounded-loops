# Clinical Note Completeness: fix output.json to match schema.json

Goal: make `output.json` (a SOAP clinical note) validate against
`schema.json`.

Steps each turn:
  1. Read `schema.json` to see the required contract: `patient_id`
     (string), `encounter_date` (string, date format), `subjective`
     (string, non-empty), `objective` (string, non-empty), `assessment`
     (string, non-empty), `plan` (string, non-empty), and `icd10_codes`
     (array of at least one string, each matching a real ICD-10-CM code
     shape). All seven fields are required; no extra properties are
     allowed.
  2. Read `output.json` and compare it against the contract.
  3. Fix every mismatch: add any missing required field with a valid
     value (in particular, a missing `plan` section is a common
     incomplete-note error — write a real treatment plan consistent
     with the `assessment`), and correct any field whose value doesn't
     satisfy its type, format, or pattern constraint (an `icd10_codes`
     entry that doesn't match the ICD-10 shape must be replaced with a
     real, correctly-shaped code — e.g. `E11.9` for type 2 diabetes
     mellitus without complications, or `I10` for essential
     hypertension).

Done when: `output.json` validates cleanly against `schema.json`
(`jsonschema.validate` raises no error).
Then output: <promise>VALID</promise>

Do not delete required fields from `schema.json`.
Do not loosen `schema.json` (e.g. removing `required` entries, the
`icd10_codes` pattern, or `additionalProperties: false`) to make the
broken note pass — the contract is the ground truth, not the note.
Do not add new dependencies.
