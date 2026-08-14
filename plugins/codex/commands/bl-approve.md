---
description: Approve a node awaiting human approval in a bounded graph run
argument-hint: <run-id> <node-id>
---

Use the bounded-loops Skill to approve a paused approval node.

Parse $ARGUMENTS as two space-separated tokens: the run_id and the node_id.
If either is missing, ask the user for the missing value.

Steps:

1. Confirm the node is in AWAITING_APPROVAL state by calling
   `graph_status(run=<run>)`. If it is not in that state, report
   the actual state and stop — approving a node that is not waiting is a no-op.

2. Call `graph_approve(run=<run>, node_id=<node_id>, decision="approved")` with
   `confirm=false` first — it returns a preview and records nothing. Show the preview,
   then call again with `confirm=true`.

3. Report the result. If approval succeeds, report the updated run status
   from the response.

4. If the run resumes, watch for the next AWAITING_APPROVAL or terminal state
   by calling bl-status. Report when the run reaches a terminal state.

Security note: approving an `approval` node authorises the downstream work the
graph defines (which may include external writes or irreversible effects). Make
sure the user has seen the downstream plan before approving. Refer them to
`bl-status` and `bl-metrics` if they are unsure what comes next.
