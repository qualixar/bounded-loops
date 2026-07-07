---
description: Preview, lint, and run a bounded loop through the local bounded-loops MCP server or CLI.
---

# Run a Bounded Loop

Loop directory: `${input:loopDir:loops/bug-fix-red-green}`

Steps:

1. Run `python3 -m bounded_loops.cli show ${input:loopDir}`.
2. Run `python3 -m bounded_loops.cli gates` and note missing dependencies.
3. Run `python3 -m bounded_loops.cli lint ${input:loopDir}`.
4. Preview the runner and gate before execution. If using MCP, call `bl_run` with `confirm=false` first.
5. Run `python3 -m bounded_loops.cli run ${input:loopDir} --yes` only after the gate and runner are understood.
6. If a persistent workspace is needed, run with `--run-id <id>` and resume with `--run-id <id> --resume`.
7. Report the exact terminal status: `DONE`, `HALT`, `PAUSE`, `KILLED`, or `ERROR`.
8. Do not summarize a failed or paused loop as success.