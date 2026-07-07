---
name: Bounded Loop Authoring
description: Use when editing bounded-loops manifests, prompts, bounds, cassettes, gates, runners, or loop examples.
applyTo: "**/{loop.yaml,bounds.yaml,PROMPT.md,STATE.md,cassettes/*.json,bounded_loops/**/*.py,loops/**/README.md}"
---

# Bounded Loop Authoring Rules

- A loop is done only when its independent gate passes. Never instruct an agent to stop because it believes the task is complete.
- Protect the verification anchor with `forbid:` patterns when the gate depends on tests, schemas, reporter data, policy files, or checker scripts.
- Keep `runner.default` keyless for committed examples unless the README explicitly marks the loop as an integration example.
- Use `require_approval: true` or rung-derived approval for production-grade regulated workflows. Use demo bypasses only when clearly documented.
- Prefer typed gates over generic `command` gates when the gate has structured output that can be parsed safely.
- Every example should prove the unfixed seed fails and the fixed workspace passes.
- Avoid personal paths, real credentials, real patient/customer data, and public claims that are not mechanically verified.