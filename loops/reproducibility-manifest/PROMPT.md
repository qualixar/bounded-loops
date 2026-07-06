# Reproducibility Manifest: fix output.json to match schema.json

Goal: make `output.json` validate against `schema.json`.

Steps each turn:
  1. Read `schema.json` to see the required contract: `seed` (integer),
     `env` (string), `data_hash` (string, 64-char lowercase hex — a SHA-256
     digest), `code_version` (string). All four are required; no extra
     properties are allowed.
  2. Read `output.json` and compare it against the contract.
  3. Fix every mismatch: add any missing required field with a valid value,
     and correct any field whose value doesn't satisfy its type or pattern
     constraint.

Done when: `output.json` validates cleanly against `schema.json`
(`jsonschema.validate` raises no error).
Then output: <promise>VALID</promise>

Do not delete required fields from `schema.json`.
Do not loosen `schema.json` (e.g. removing `required` entries, the
`data_hash` pattern, or `additionalProperties: false`) to make the broken
data pass — the contract is the ground truth, not the data.
Do not add new dependencies.
