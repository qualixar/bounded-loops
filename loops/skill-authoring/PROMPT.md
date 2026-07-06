# Skill Authoring: make the skill manifest conform to schema

Goal: make `output.json` conform to `schema.json` — the required shape for
an authored agent-skill's metadata/frontmatter before it is considered
publishable.

Steps each turn:
  1. Read `output.json` and note which fields are missing or invalid
     against `schema.json` (required: `name`, `description`,
     `allowed_tools`).
  2. `name` is `"My Cool Skill"`, which is not kebab-case — rewrite it as
     lowercase, hyphen-separated, matching `^[a-z][a-z0-9-]*$`
     (e.g. `"my-cool-skill"`).
  3. `description` is `"does stuff"`, which is under the required 20
     characters and not substantive — replace it with a real explanation
     of what the skill does and when to use it (at least 20 characters).
  4. Leave `allowed_tools` as-is; it already conforms (non-empty array of
     strings).

Done when: `output.json` validates cleanly against `schema.json`.
Then output: <promise>SCHEMA_VALID</promise>

Do NOT loosen, edit, or otherwise touch `schema.json` to make the broken
manifest pass — fix the instance (`output.json`), not the schema.
Do not add new top-level properties (the schema forbids additional
properties).
