# bounded-loops Agent Guide

This repository builds a portable AI loop harness for developer agents. Treat it as production infrastructure, not a prompt collection.

## Core Invariant

The agent never grades itself. `agent_claimed_done` is advisory metadata only. A loop reaches `DONE` only when the independent gate passes and required approval is satisfied.

## Architecture Rules

- Domain code stays pure: no I/O, no subprocesses, no framework imports.
- Application code coordinates ports and domain rules only.
- Adapters implement concrete runners, gates, and I/O ports.
- `composition.py` is the only place that wires concrete adapters.
- Tests should verify behavior by running the narrowest relevant command.

## Loop Authoring Rules

- Each committed loop needs `loop.yaml`, `bounds.yaml`, `PROMPT.md`, `README.md`, `seed/`, and a real gate.
- Keyless examples should use `stub`, `shell`, or `python_callable` runners only.
- Production examples in regulated domains should not imply autonomous acceptance. Use approval gates or document demo-only bypasses clearly.
- Protect gate anchors with `forbid:` patterns.
- Prefer structured gates and parsed evidence over exit-code-only commands when possible.

## Common Commands

```bash
python3 -m bounded_loops.cli list
python3 -m bounded_loops.cli show loops/bug-fix-red-green
python3 -m bounded_loops.cli gates
python3 -m bounded_loops.cli lint loops/bug-fix-red-green
python3 -m bounded_loops.cli run loops/bug-fix-red-green --yes
python3 -m bounded_loops.cli run loops/bug-fix-red-green --yes --run-id demo
python3 -m bounded_loops.cli run loops/bug-fix-red-green --yes --run-id demo --resume
python3 -m bounded_loops.cli runs loops/bug-fix-red-green
pytest -q
```

Use `--keep-workspace` only for debugging a run. Normal runs should clean their scratch workspace.