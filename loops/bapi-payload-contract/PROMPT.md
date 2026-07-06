# BAPI Payload Contract: fix output.json to match schema.json

Goal: make `output.json` validate against `schema.json`.

Steps each turn:
  1. Read `schema.json` to see the required contract for a
     `BAPI_MATERIAL_SAVEDATA`-style payload: `HEADDATA` (object) requires
     `MATERIAL` (string), `IND_SECTOR` (string), and `MATL_TYPE` (string);
     `CLIENTDATA` (object) requires `BASE_UOM` (string). Both top-level
     keys are required; no extra properties are allowed anywhere.
  2. Read `output.json` and compare it against the contract.
  3. Fix every mismatch: add any missing required field with a valid
     value consistent with the rest of the payload.

Done when: `output.json` validates cleanly against `schema.json`
(`jsonschema.validate` raises no error).
Then output: <promise>VALID</promise>

Do not delete required fields from `schema.json`.
Do not loosen `schema.json` (e.g. removing `required` entries or any
`additionalProperties: false`) to make the broken data pass — the
contract is the ground truth, not the data.
Do not add new dependencies.
