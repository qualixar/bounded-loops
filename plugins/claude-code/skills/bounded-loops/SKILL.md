---
name: bounded-loops
description: Run, lint, or list bounded AI-agent loops (bounded-loops engine). Use when the user asks to "run a bounded loop", references a loop.yaml, or asks to verify a task against an independent gate rather than trusting the agent's own claim.
---

# bounded-loops

`bounded-loops` runs a loop made of a goal, runner, independent gate, bounds,
memory, and ledger. The central invariant is strict: the agent never decides its
own completion. `agent_claimed_done` is audit metadata only. A loop is complete
only when the independent gate passes and required approval is satisfied.

Use the MCP tools when available. Use the CLI fallback when MCP is unavailable.
Do not bypass the preview/confirmation model for loop execution.

## Engine model

- `loop.yaml`: manifest: name, pattern, role, rung, runner, gate, forbid rules.
- `bounds.yaml`: max iterations, no-progress window, token budget, wall-clock,
   sandbox, input quarantine, schema, tracing, approval posture.
- `PROMPT.md`: the per-lap task specification handed to the runner.
- `STATE.md`: loop-scoped memory outside the agent-writable scratch workspace.
- `seed/`: broken starting artifact copied into the scratch workspace.
- `cassettes/default.json`: deterministic stub runner turns for keyless demos.
- `.ledger.jsonl`: append-only run ledger at the loop directory.

Terminal statuses:

- `DONE`: gate passed and approval was granted or not required.
- `HALT`: safety bound tripped, such as max iterations or no progress.
- `PAUSE`: gate passed but approval is required and not granted.
- `KILLED`: external kill switch tripped.
- `ERROR`: runner or gate failed to execute; inspect ledger evidence.

## MCP procedure

Available tools:

- `bl_list()`: discover loops.
- `bl_lint(loop_dirs=[...])`: validate manifests and bounds.
- `bl_show(loop_dir=...)`: inspect runner, gate, bounds, dependencies, risk tags,
  production bounds, and content hash.
- `bl_gates()`: list gate kinds and local dependency availability.
- `bl_audit_loops(dirs=[...])`: audit loop examples for copy-paste production readiness.
- `bl_run(loop_dir=..., confirm=false, runner?, gate_override?, max_iterations?)`: preview.
- `bl_run(loop_dir=..., confirm=true, ...)`: execute only after matching preview.

When running a loop:

1. Use `bl_list()` if the loop path is not obvious.
2. Use `bl_show(loop_dir=loop_dir)` to inspect runner, gate, dependencies, risk,
   and production bounds.
3. Use `bl_lint(loop_dirs=[loop_dir])` before running.
4. Call `bl_run(..., confirm=false)` and show the user the runner, gate,
    `agent_cmd`, and cassette.
5. Call `bl_run(..., confirm=true)` only with the same arguments.
6. Report the exact returned status. Never convert `HALT`, `PAUSE`, `KILLED`,
    or `ERROR` into success language.

The server intentionally rejects `confirm=true` without a matching preview.
Treat that as a safety feature, not a bug.

## CLI fallback

Use these commands when MCP is unavailable:

```bash
python3 -m bounded_loops.cli list
python3 -m bounded_loops.cli show loops/<name>
python3 -m bounded_loops.cli gates
python3 -m bounded_loops.cli lint loops/<name>
python3 -m bounded_loops.cli run loops/<name> --yes
python3 -m bounded_loops.cli run loops/<name> --yes --keep-workspace
python3 -m bounded_loops.cli run loops/<name> --yes --run-id <id>
python3 -m bounded_loops.cli run loops/<name> --yes --run-id <id> --resume
python3 -m bounded_loops.cli runs loops/<name>
python3 -m bounded_loops.cli audit-loops
```

Use `--keep-workspace` only for debugging. Normal runs clean their scratch
workspace after terminal outcome.
Use `--run-id` when the user needs a persistent workspace, per-run ledger, and
metadata under `.bounded-loops/runs/<id>/`.

## Gate discipline

- The gate is the verifier. The runner is only a proposer.
- Prefer typed gates (`pytest`, `jsonschema`, `osv`, `checkov`, `composite`) over
   generic `command` when the output can be parsed.
- `command` gates run with `shell=False`; commands needing shell features should
   use a checked-in wrapper script.
- `composite` gates in v1 support `mode: all`; all child gates must pass.
- Missing tools, scanner crashes, empty security reports, and malformed gate
   output are not clean passes.

## Authoring or editing loops

Every production-grade loop should have:

- A real mechanical gate.
- A broken `seed/` artifact.
- A README with failure proof, success proof, limitations, and production usage.
- `forbid:` protection for tests, schemas, reporter data, policy files, or checker scripts.
- Bounds that match the risk of the domain.
- Approval enabled or clearly documented for regulated L2/L3 production use.

Validation checklist:

```bash
python3 -m bounded_loops.cli lint loops/<name>
python3 -m bounded_loops.cli run loops/<name> --yes
python3 -m bounded_loops.cli audit-loops loops/<name>
```

## Reporting rules

- Say `DONE` only when the returned status is exactly `DONE`.
- If status is `ERROR`, name whether the runner or gate failed and quote the
   short error detail.
- If status is `HALT`, explain which bound tripped.
- If status is `PAUSE`, explain what approval is needed.
- Include the ledger path for debugging.

## When the user asks to run a bounded loop

Follow the MCP procedure above.

## When the user asks to lint a loop

Call `bl_lint(loop_dirs=[...])`. Report `all_passed` and any per-entry errors.

## When the user asks what loops exist

Call `bl_list()` with no arguments.

## When the user asks whether a loop is safe to run

Call `bl_show(loop_dir=...)` and `bl_gates()` before any preview or execution.
Name missing dependencies, local command gates, demo approval bypasses, and
whether `bounds.production.yaml` exists.
