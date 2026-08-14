---
description: Show the current state of a bounded graph run
argument-hint: <run-id>
---

Use the bounded-loops Skill to report the status of the graph run identified
by $ARGUMENTS.

Steps:

1. Call `bl_graph_status(run_id="$ARGUMENTS")`.

2. Report the exact terminal state using these mappings — do not paraphrase:

   | Returned status | What to tell the user |
   |---|---|
   | SUCCEEDED | "Run $ARGUMENTS succeeded. All nodes completed." |
   | FAILED | "Run $ARGUMENTS failed. Node <node_id> failed at attempt <n>: <error>. Review receipts." |
   | HALTED | "Run $ARGUMENTS halted. A budget or kill-switch tripped: <reason>." |
   | CANCELLED | "Run $ARGUMENTS was cancelled." |
   | EXPIRED | "Run $ARGUMENTS expired (max_wallclock_s exceeded)." |
   | RUNNING | "Run $ARGUMENTS is still active. Current node: <node_id> at attempt <n>." |
   | AWAITING_APPROVAL | "Run $ARGUMENTS is paused at node <node_id> awaiting approval. Use bl-approve to continue." |

3. If the status is not SUCCEEDED, include the receipt path for debugging.

NEVER convert FAILED, HALTED, CANCELLED, or EXPIRED into success language or
call the outcome "partial success." Report the exact status verbatim.

If run_id is missing or not found, say so clearly and suggest listing runs
with `python3 -m bounded_loops.graph.cli_graph status --list`.
