---
name: bounded-loops
description: Run, lint, or list bounded AI-agent loops (bounded-loops engine). Use when the user asks to "run a bounded loop", references a loop.yaml, or asks to verify a task against an independent gate rather than trusting the agent's own claim.
---

# bounded-loops

`bounded-loops` runs a loop made of a goal, runner, independent gate, bounds,
memory, and ledger. The central invariant is strict: the agent never decides its
own completion. `agent_claimed_done` is audit metadata only. A loop is complete
only when the independent gate passes and required approval is satisfied.

Use MCP tools when available. Use the CLI fallback when MCP is unavailable.

## Operating procedure

1. Discover with `bl_list()` or `python3 -m bounded_loops.cli list`.
2. Inspect with `bl_show(loop_dir=...)` or `python3 -m bounded_loops.cli show loops/<name>`.
3. Check dependencies with `bl_gates()` or `python3 -m bounded_loops.cli gates`.
4. Validate with `bl_lint(loop_dirs=[...])` or `python3 -m bounded_loops.cli lint loops/<name>`.
5. Preview with `bl_run(confirm=false)` before execution.
6. Execute with `bl_run(confirm=true)` only after the preview is accepted.
7. Report exact status: `DONE`, `HALT`, `PAUSE`, `KILLED`, or `ERROR`.

## CLI fallback

```bash
python3 -m bounded_loops.cli list
python3 -m bounded_loops.cli show loops/<name>
python3 -m bounded_loops.cli gates
python3 -m bounded_loops.cli lint loops/<name>
python3 -m bounded_loops.cli run loops/<name> --yes
python3 -m bounded_loops.cli run loops/<name> --yes --run-id <id>
python3 -m bounded_loops.cli run loops/<name> --yes --run-id <id> --resume
python3 -m bounded_loops.cli runs loops/<name>
python3 -m bounded_loops.cli audit-loops
```

Use `--keep-workspace` only for debugging.
Use `--run-id` for resumable persistent workspaces and per-run ledgers.

## Gate and bounds facts

- The gate is the verifier. The runner is only a proposer.
- `command` gates run without a shell.
- `pytest`, `jsonschema`, `osv`, `checkov`, and `composite` are first-class gate kinds.
- `composite` supports `mode: all` in v1.
- L2/L3 production loops should require approval unless the README explicitly marks the bypass as demo-only.

## When the user asks to run a bounded loop

Follow the operating procedure above.

## When the user asks to lint a loop

Call `bl_lint(loop_dirs=[...])`. Report `all_passed` and any per-entry errors.

## When the user asks what loops exist

Call `bl_list()` with no arguments.
