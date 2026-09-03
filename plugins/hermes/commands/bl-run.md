---
description: Preview and run a bounded loop
argument-hint: <loop-dir>
---

Use the bounded-loops Skill to preview and then run the loop at $ARGUMENTS.

Always follow the two-step pattern — do not skip the preview even if the
user says "just run it":

1. **Preview** — call `bl_run(loop_dir="$ARGUMENTS", confirm=false)`.
   Show the user: runner command, gate kind, max_attempts, max_wallclock_s,
   risk tags, and production_ready status. If the loop is not production-ready,
   say so and wait for explicit confirmation before proceeding.
   **Keep the `confirm_token` from this response.** You need it in step 2.

2. **Execute** — only after the user confirms ("yes", "run it", "go", etc.),
   call `bl_run(loop_dir="$ARGUMENTS", confirm=true, confirm_token="<the token
   from step 1>")`.

The token is required and expires after 15 minutes. It is bound to the exact
arguments you previewed, so if you change the runner, gate, or iteration cap
between the two calls it stops working — preview again rather than trying to
force it through.

Report the terminal status exactly as returned — DONE, HALT, ERROR, PAUSE, or
KILLED. DONE is the only success status. All others require investigation:

- HALT: a bound tripped (attempts, wallclock, tokens). Report which bound.
- ERROR: the runner or gate threw an exception. Report the error detail.
- PAUSE: an approval node was reached. Use bl-approve to continue.
- KILLED: an external kill signal was received.

Never describe a HALT, ERROR, PAUSE, or KILLED outcome as "complete",
"finished", or "partial success."

The server enforces the two-step: it refuses confirm=true without a matching
preview. If you see that error, run the preview step first.
