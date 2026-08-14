---
description: Show token spend, cost, and timing per node in a bounded graph run
argument-hint: <run-id>
---

Use the bounded-loops Skill to report resource usage for the graph run
identified by $ARGUMENTS.

Steps:

1. Call `bl_graph_metrics(run_id="$ARGUMENTS")`.

2. Present the results in a table with columns:
   node_id | status | tokens_in | tokens_out | cost_microunits | wallclock_s

3. After the table, report the run totals:
   - Total tokens (in + out)
   - Total cost in microunits (and converted to the user's preferred unit if
     known)
   - Total elapsed wall time

4. Flag any node where:
   - tokens_in + tokens_out ≥ 90% of its declared max_tokens budget
   - cost_microunits ≥ 90% of its declared max_cost_microunits budget
   - wallclock_s ≥ 90% of its declared max_wallclock_s budget
   These nodes ran close to their bounds; a repeat run may trip SPEND_EXHAUSTED
   or timeout.

5. If a node returned BUDGET_UNMEASURABLE (the worker returned no usage data),
   note it explicitly. That node is not metered as free — the engine treats
   unmeasurable spend as a policy breach.

If run_id is missing or not found, say so clearly.
