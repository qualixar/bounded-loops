# Agent Authoring: make the sub-agent config conform to schema

Goal: make `output.json` conform to `schema.json` — the required shape for
an authored sub-agent config before it may be registered.

Steps each turn:
  1. Read `output.json` and note which fields are missing or invalid
     against `schema.json` (required: `name`, `description`, `tools`,
     `model`).
  2. `tools` is missing entirely — add a non-empty array of tool names
     drawn only from `["Read", "Write", "Edit", "Bash", "Grep", "Glob"]`,
     sized to whatever the sub-agent genuinely needs.
  3. `model` is set to `"gpt-4"`, which is not one of the allowed values —
     set it to one of `"sonnet"`, `"opus"`, `"haiku"`.
  4. Leave `name` and `description` as-is; they already conform.

Done when: `output.json` validates cleanly against `schema.json`.
Then output: <promise>SCHEMA_VALID</promise>

Do NOT loosen, edit, or otherwise touch `schema.json` to make the broken
config pass — fix the instance (`output.json`), not the schema.
Do not add new top-level properties (the schema forbids additional
properties).
