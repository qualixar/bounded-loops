---
description: List, show, or lint bounded loop packages in the workspace
argument-hint: "[list | show <loop-dir> | lint <loop-dir>]"
---

Use the bounded-loops Skill to inspect bounded loop packages.

Parse $ARGUMENTS to select the sub-command:

- **No arguments or `list`** — call `bl_list()`. Show each package: name,
  runner kind, gate kind, production_ready flag, and risk tags. If a package
  is not production-ready, say so explicitly and name the missing properties.

- **`show <loop-dir>`** — call `bl_show(loop_dir=<loop-dir>)`. Display:
  runner command, gate kind, gate configuration, all bound fields
  (max_attempts, max_wallclock_s, max_tokens if present), risk tags, and
  production_ready status.

- **`lint <loop-dir>`** — call `bl_lint(loop_dirs=[<loop-dir>])`. If lint
  passes, say "lint passed — manifest and bounds are valid." If lint fails,
  list each error with the field path and the fix.

Default: treat bare $ARGUMENTS as a `show` target if it looks like a path
(starts with `/`, `./`, or `../`), otherwise `list`.

Production readiness rule: do not recommend executing a loop marked
`production_ready: false` without confirming the user understands the
implication. Non-production loops may lack risk tags, have unbounded retries,
or carry effects that are not idempotency-keyed.

Gate discipline: if a loop uses a `command` gate, check that the command is
not an LLM invocation that asks the model to verify its own output. An LLM
self-check is not an independent gate. Flag it if present.
