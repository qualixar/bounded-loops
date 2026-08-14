---
description: Compose, validate, and plan a bounded graph — stop before execute
argument-hint: "<task description>"
---

Use the bounded-loops Skill to compose a bounded graph for the task described
in $ARGUMENTS.

Steps (follow in order, stop at step 4):

1. **Compose** — call `bl_graph(task="$ARGUMENTS")` to draft a graph manifest.
   The tool lints the manifest against the engine's schema and validator rules
   and returns either a validated plan or a list of validator errors.

2. **Fix** — if the composer returns errors (refusal codes), look each code up
   in the refusal reference (plugins/shared/docs/refusal-reference.md), fix the
   manifest, and retry. Loop until lint is clean. Never deliver a manifest with
   open errors.

3. **Show the plan** — display:
   - Every node: id, kind, isolation tier, budget (max_attempts, max_wallclock_s),
     declared effects
   - Every edge: source port → target port, any when-condition
   - Gate kind for every loop node
   - Any `customer_managed_worker` isolation nodes (flag: cannot run anywhere)
   - Any `on_failure_unimplemented` policies in the plan (flag: will be refused)

4. **STOP.** Do NOT call any execute command. Do NOT call `bl_graph_run`,
   `bl_graph compile --execute`, or any equivalent. The user must review the
   plan and explicitly say "run it", "go", or "execute" before any work starts.

Why stop before execute: graph nodes may carry external_write, financial, or
irreversible effects. Showing the plan is free; reversing an executed effect is
not. The two-step pattern (compose → approve → execute) is the safety contract.

After the user approves, run `bl_graph_run(plan_id=<plan_id>)` and report the
run_id for tracking with bl-status.
