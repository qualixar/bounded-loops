# ISO 20022 Payment: fix output.json to match schema.json

Goal: make `output.json` validate against `schema.json`.

Steps each turn:
  1. Read `schema.json` to see the required contract: `msgId` (string),
     `amount` (number, strictly greater than 0), `currency` (string, enum
     EUR/USD/GBP), `debtorIban` (string), `creditorIban` (string). All five
     are required; no extra properties are allowed.
  2. Read `output.json` and compare it against the contract.
  3. Fix every mismatch: add any missing required field with a valid
     value, and correct any field whose value doesn't satisfy its type,
     range, or enum constraint.

Done when: `output.json` validates cleanly against `schema.json`
(`jsonschema.validate` raises no error).
Then output: <promise>VALID</promise>

Do not delete required fields from `schema.json`.
Do not loosen `schema.json` (e.g. removing `required` entries, the
`currency` enum, the `exclusiveMinimum` on `amount`, or
`additionalProperties: false`) to make the broken data pass — the
contract is the ground truth, not the data.
Do not add new dependencies.
