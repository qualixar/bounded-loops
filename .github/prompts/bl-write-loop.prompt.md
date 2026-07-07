---
description: Design a new production-grade bounded loop with a real mechanical gate.
---

# Write a Production-Grade Bounded Loop

Loop name: `${input:loopName:my-loop}`
Domain: `${input:domain:software}`
Gate type: `${input:gateType:pytest}`

Create or revise a loop so that it has:

- A testable goal in `PROMPT.md`.
- A broken `seed/` that demonstrates the failure.
- A real mechanical gate in `loop.yaml`.
- Bounds that match the risk of the domain.
- `forbid:` protection for the gate anchor.
- A deterministic keyless runner path when feasible.
- A README with failure proof, success proof, production adaptation notes, and limitations.

Validate with:

```bash
python3 -m bounded_loops.cli lint loops/${input:loopName}
python3 -m bounded_loops.cli run loops/${input:loopName} --yes
```