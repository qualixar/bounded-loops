# Roadmap Field Contract: fix output.json to match schema.json

Goal: make `output.json` validate against `schema.json`.

Steps each turn:
  1. Read `schema.json` to see the required contract: `title` (string),
     `owner` (string), `quarter` (string, enum Q1/Q2/Q3/Q4), `status`
     (string, enum planned/in_progress/done). All four are required; no
     extra properties are allowed.
  2. Read `output.json` and compare it against the contract.
  3. Fix every mismatch: add any missing required field with a valid
     value, and correct any field whose value doesn't satisfy its type,
     format, or enum constraint.

Done when: `output.json` validates cleanly against `schema.json`
(`jsonschema.validate` raises no error).
Then output: <promise>VALID</promise>

Do not delete required fields from `schema.json`.
Do not loosen `schema.json` (e.g. removing `required` entries, the
`quarter`/`status` enums, or `additionalProperties: false`) to make the
broken data pass — the contract is the ground truth, not the data.
Do not add new dependencies.
