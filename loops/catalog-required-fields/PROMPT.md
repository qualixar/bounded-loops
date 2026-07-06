# Catalog Required Fields: fix output.json to match schema.json

Goal: make `output.json` validate against `schema.json`.

Steps each turn:
  1. Read `schema.json` to see the required contract: `title` (string),
     `price` (number), `image` (string), `description` (string). All four
     are required; no extra properties are allowed.
  2. Read `output.json` and compare it against the contract.
  3. Fix every mismatch: add any missing required field with a valid value.

Done when: `output.json` validates cleanly against `schema.json`
(`jsonschema.validate` raises no error).
Then output: <promise>VALID</promise>

Do not delete required fields from `schema.json`.
Do not loosen `schema.json` (e.g. removing `required` entries or
`additionalProperties: false`) to make the broken data pass — the contract
is the ground truth, not the data.
Do not add new dependencies.
