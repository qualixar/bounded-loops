"""bounded_loops/hooks/graph_run_stop.py — graph-run-active stop hook.

Fires on a Stop event from Claude Code, Codex, or Antigravity.

If the current workspace has any bounded GRAPH run that is NOT in a terminal
state (SUCCEEDED, FAILED, HALTED, CANCELLED, EXPIRED), the hook blocks the
host from declaring the session "done" — the user must wait for the run to
finish, cancel it, or resume it before ending the session.

Terminal status is read directly from the receipt log
(controller-events.jsonl) in each run directory, never from a cache.
`index.json` is explicitly NOT consulted — it is a rebuildable cache, not
authority (workspace.py module docstring).

Protocol: identical to verify_bounded_loop.py.
  Claude Code / Codex: exit 0 = allow, exit 2 = deny + stderr reason.
  Antigravity: JSON {"decision": "allow"|"deny", "reason": str} on stdout
               + exit 0 (allow) or exit 1 (deny).

Fail-open rule: any exception in workspace discovery, file reading, or state
parsing causes the hook to allow — a buggy hook must never produce a false
block that strands the user.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Run-level states that are terminal — a run in any of these is done.
# Source: bounded_loops/graph/adapters/persistence/event_log.py:32
# _TERMINAL = frozenset({"SUCCEEDED", "FAILED", "HALTED", "CANCELLED", "EXPIRED"})
_TERMINAL_RUN_STATES = frozenset({"SUCCEEDED", "FAILED", "HALTED", "CANCELLED", "EXPIRED"})

# Event types that directly set run state.
# Source: event_log.py _apply() function.
_STATE_SETTING_EVENTS = {
    "run.started": "RUNNING",
    "run.succeeded": "SUCCEEDED",
    "run.failed": "FAILED",
    "run.cancelled": "CANCELLED",
    "run.halted": "HALTED",
    "run.expired": "EXPIRED",
}

EVENTS_FILENAME = "controller-events.jsonl"
WORKSPACE_DIRNAME = ".bounded-loops"
RUNS_SUBDIR = "runs"


def _extract_cwd(payload: dict, tool: str) -> str | None:
    """Extract the working directory from the hook payload.

    Field name differs per host — mirrors verify_bounded_loop.py.
    """
    if tool in ("claude-code", "codex"):
        return payload.get("cwd")
    if tool == "antigravity":
        paths = payload.get("workspacePaths") or []
        return paths[0] if paths else None
    return None


def _read_run_state(run_dir: Path) -> str | None:
    """Return the last known run state from controller-events.jsonl, or None.

    Reads the receipt log directly — never a cache.  Skips unrecognised or
    malformed lines (fail-open): a corrupt log is not this hook's problem to
    police, and blocking on it would be a false denial.
    """
    events_path = run_dir / EVENTS_FILENAME
    if not events_path.is_file():
        return None
    try:
        text = events_path.read_text(encoding="utf-8")
    except OSError:
        return None

    last_state: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue  # skip malformed lines, fail-open
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        if event_type in _STATE_SETTING_EVENTS:
            last_state = _STATE_SETTING_EVENTS[event_type]
    return last_state


def _check_workspace(project_root: Path) -> tuple[bool, str]:
    """Check whether any run in the workspace is in a non-terminal state.

    Returns (passed, reason).  passed=True means allow session-stop.
    """
    runs_dir = project_root / WORKSPACE_DIRNAME / RUNS_SUBDIR
    if not runs_dir.is_dir():
        return True, "no runs directory — no active bounded graph runs"

    active: list[str] = []
    try:
        entries = sorted(runs_dir.iterdir())
    except OSError:
        return True, "could not read runs directory — allowing"

    for entry in entries:
        if not entry.is_dir():
            continue
        state = _read_run_state(entry)
        if state is None:
            # Unknown state (no events file, or no state events) — fail-open.
            continue
        if state not in _TERMINAL_RUN_STATES:
            active.append(f"{entry.name} ({state})")

    if active:
        names = ", ".join(active)
        return False, (
            f"bounded graph run(s) still active: {names}. "
            "Wait for them to finish, cancel them with `bl graph cancel`, "
            "or resume them. A non-terminal run is never 'done'."
        )
    return True, "no active bounded graph runs"


def _discover_project_root(cwd: str) -> Path | None:
    """Walk upward from cwd to find the nearest .bounded-loops/ directory.

    Mirrors workspace.discover() semantics but avoids importing the full
    workspace module so this hook has minimal dependencies and stays fast.

    Returns the project root (parent of .bounded-loops/), or None if no
    workspace is found.
    """
    try:
        start = Path(cwd)
        if not start.is_absolute() or start.is_symlink():
            return None
        resolved = start.resolve()
        if not resolved.is_dir():
            return None
    except (TypeError, ValueError, OSError):
        return None

    for candidate in (resolved, *resolved.parents):
        ws = candidate / WORKSPACE_DIRNAME
        if ws.is_dir() and not ws.is_symlink():
            return candidate
    return None


def main(argv: list[str]) -> int:
    tool = argv[1] if len(argv) > 1 else "claude-code"
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, ValueError):
        return 0  # malformed payload — fail open
    if not isinstance(payload, dict):
        return 0

    cwd_str = _extract_cwd(payload, tool)
    if cwd_str is None:
        return 0  # can't determine directory — allow

    project_root = _discover_project_root(cwd_str)
    if project_root is None:
        return 0  # no workspace found — allow

    passed, reason = _check_workspace(project_root)

    if tool == "antigravity":
        decision = "allow" if passed else "deny"
        print(json.dumps({"decision": decision, "reason": reason}))
        return 0 if passed else 1

    # Claude Code / Codex: exit-code protocol.
    if not passed:
        print(reason, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
