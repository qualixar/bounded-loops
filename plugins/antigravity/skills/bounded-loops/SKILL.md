---
name: bounded-loops
description: Run, lint, or list bounded AI-agent loops (bounded-loops engine). Use when the user asks to "run a bounded loop", references a loop.yaml, or asks to verify a task against an independent gate rather than trusting the agent's own claim.
---

# bounded-loops

`bounded-loops` runs a loop (spec + runner + independent gate + memory) with nine
safety bounds. The gate is ALWAYS the ground truth — never report a loop DONE
because you believe the work is finished; only `bl_run`'s returned `status`
field decides that.

## When the user asks to run a bounded loop

1. Call the `bl_list` MCP tool to discover available loops (or ask the user
   for a `loop_dir` if none exist yet).
2. Call `bl_run(loop_dir=..., confirm=false)` FIRST — this returns a preview
   of the exact runner and gate command that would execute. Show this to
   the user before proceeding; never skip straight to confirm=true.
3. Only after the user has seen and accepted the preview, call
   `bl_run(loop_dir=..., confirm=true, ...)` with the SAME arguments. This
   will be rejected if the arguments differ from what was just previewed —
   that's intentional (server-enforced), not a bug to work around. A successful
   confirm=true run also records a trust entry that the verify-on-stop
   hook will later recognize for this exact loop_dir + gate command.
4. Report the returned `status` verbatim (DONE/HALT/PAUSE/KILLED) — never
   paraphrase a HALT/PAUSE as if it were a success.

## When the user asks to lint a loop

Call `bl_lint(loop_dirs=[...])`. Report `all_passed` and any per-entry errors.

## When the user asks what loops exist

Call `bl_list()` with no arguments.
