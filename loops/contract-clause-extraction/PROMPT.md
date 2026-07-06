# Contract Clause Extraction: fix output.json to match schema.json

Goal: make `output.json` validate against `schema.json`.

Steps each turn:
  1. Read `schema.json` to see the required contract: top-level
     `contract_title` (string), `parties` (array of string, at least 2
     items), and `clauses` (object). `clauses` requires ALL FOUR keys:
     `termination`, `indemnification`, `governing_law`, `confidentiality`.
     Each clause value must be an object with required `present` (boolean)
     and `text` (string). No extra properties are allowed anywhere.
  2. Read `output.json` and compare it against the contract.
  3. Fix every mismatch: add any missing required clause with a valid
     `{present, text}` object, and correct any clause whose value doesn't
     match that shape.

Done when: `output.json` validates cleanly against `schema.json`
(`jsonschema.validate` raises no error).
Then output: <promise>VALID</promise>

Do not delete required fields from `schema.json`.
Do not loosen `schema.json` (e.g. removing `required` entries, the
`clauses` sub-schema, or `additionalProperties: false`) to make the broken
data pass — the contract is the ground truth, not the data.
Do not add new dependencies.
