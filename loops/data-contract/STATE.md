# Loop State — data-contract

## Session start
- Target: `seed/output.json`
- Gate: `output.json` validated against `schema.json` via JsonSchemaGate (must pass)
- Laps completed: 0
- Last verdict: not yet run

## Instructions for the agent
Each lap, before editing, check what the last schema-validation verdict said.
Record what you changed and why in the "Lap log" section below.
Do not repeat a fix you already tried.

## Lap log
(populated by the loop engine each lap via MemoryPort.update)
