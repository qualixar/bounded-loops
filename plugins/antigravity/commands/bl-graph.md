---
description: Compose, validate, and plan a bounded graph — stop before execute
argument-hint: "<task description>"
---

Use the bounded-loops Skill to compose a bounded graph for the task described
in $ARGUMENTS.

Steps (follow in order, stop at step 4):

1. **Compose** — find reusable loops with `bl_search_loops(task_description="$ARGUMENTS")`
   and read `bl_capabilities()` for what this host can actually enforce. Write the
   manifest, then call `graph_lint(manifest_yaml=...)` and `graph_plan(manifest_yaml=...)`.
   Lint returns validator errors; plan returns the compiled node and edge list.

   There is no tool that turns a sentence into a graph. Composition is yours: the
   engine validates what you wrote and refuses what it cannot enforce.

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

4. **STOP.** Do NOT call `graph_run`, `bl graph run --execute`, or any equivalent.
   The user must review the plan and explicitly say "run it", "go", or "execute"
   before any work starts.

Why stop before execute: graph nodes may carry external_write, financial, or
irreversible effects. Showing the plan is free; reversing an executed effect is
not. The two-step pattern (compose → approve → execute) is the safety contract.

After the user approves, call `graph_run(manifest_yaml=...)` and report the run name
for tracking with `/bl-status`.
