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

# Imported, not copied. A hardcoded copy of this set is the failure mode that matters most here:
# a terminal state the hook does not recognise reads as "still active", so the hook would block
# every session in that workspace forever, and the user's only recourse would be to remove the
# plugin. `tests/test_graph_run_stop_hook.py` additionally pins the event map below against the
# log's own transitions.
from bounded_loops.graph.adapters.persistence.event_log import (  # noqa: E402
    _TERMINAL as _TERMINAL_RUN_STATES,
)
from bounded_loops.workspace import Workspace  # noqa: E402

# Event types that set run state, with the state each one declares.
#
# `event_payloads._state(payload, expected)` REQUIRES the payload's state to equal the literal
# the log expects for that event type — it raises otherwise — so this mapping is exact rather
# than a guess about what a payload might contain.
#
# Note what is absent: there is no `run.halted` or `run.expired` event. HALTED and EXPIRED are in
# the terminal set but no event type produces them, so inventing those two names (an earlier draft
# did, by inference from the terminal set) added two entries that could never fire.
_STATE_SETTING_EVENTS = {
    "run.started": "RUNNING",
    "run.succeeded": "SUCCEEDED",
    "run.failed": "FAILED",
    "run.cancelled": "CANCELLED",
}

EVENTS_FILENAME = "controller-events.jsonl"


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
    # Refuse a symlinked receipt log rather than following it, matching `run_store` and
    # `for_run_dir`. Both auditors flagged this: a symlink under `runs/` turns a hook that runs on
    # every Stop event into a reader of an arbitrary file. Returning None here means "unknown
    # state", which allows the stop — the safe direction for a file we refuse to trust.
    if events_path.is_symlink() or run_dir.is_symlink():
        return None
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
        # The field is `event_type`. It was read as `type` until an audit checked the hook against
        # a log the engine had actually written: `type` is absent from every real receipt, so this
        # returned None for every run, `_check_workspace` skipped every run, and the hook ALWAYS
        # allowed. The control was inert in production while 35 tests passed against a
        # hand-written fixture that used the wrong key. Hence `test_the_hook_blocks_a_run_the
        # _REAL_ENGINE_produced`, which runs the engine rather than describing it.
        event_type = event.get("event_type")
        if event_type in _STATE_SETTING_EVENTS:
            last_state = _STATE_SETTING_EVENTS[event_type]
    return last_state


def _check_workspace(project_root: Path) -> tuple[bool, str]:
    """Check whether any run in the workspace is in a non-terminal state.

    Returns (passed, reason).  passed=True means allow session-stop.
    """
    # Derived from `Workspace`, not rebuilt from local copies of `.bounded-loops` and `runs`.
    # Those two constants were re-declared here, so a rename of either would have left this hook
    # looking in a directory that no longer exists — and an empty runs directory reads as "nothing
    # active", which ALLOWS session stop. The failure would have been silent and permissive, which
    # is the same argument the `_TERMINAL` import above is making.
    runs_dir = Workspace(project_root=project_root, origin="explicit").runs_dir
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
            f"bounded graph run(s) still active: {names}. A non-terminal run is never 'done'.\n"
            "  To make this a warning instead of a block, set in .bounded-loops/config.toml:\n"
            "    [hooks]\n"
            "    stop_on_active_run = false\n"
            "  - paused on an approval? `bl graph approve --run <dir> --node <id> --decision ...`\n"
            "  - paused on a spend ceiling, or interrupted? `bl graph resume --run <dir>`\n"
            "  - abandoned for good? delete that run directory; the receipt log is the run, so\n"
            "    removing it is the only way to retire one, and it is deliberately explicit.\n"
            f"  Inspect first with: bl graph status --run {runs_dir}/<run-id>"
        )
    return True, "no active bounded graph runs"


def _discover_project_root(cwd: str) -> Path | None:
    """The workspace root for `cwd`, via the one resolver every surface uses.

    This deliberately imports `bounded_loops.workspace.discover` rather than re-walking the
    tree here. A hand-rolled walk-up looks equivalent and is not: `discover()` stops at the git
    repository root, so a checkout can never block on runs belonging to a workspace sitting
    ABOVE it, and it honours `$BOUNDED_LOOPS_WORKSPACE`. A second implementation of "where does
    this project keep its runs" would silently guard the wrong directory — which for a hook that
    exists to prevent false "done" claims means guarding nothing at all.

    Returns the project root (the parent of `.bounded-loops/`), or None if there is no workspace
    to check.
    """
    try:
        start = Path(cwd)
        if not start.is_absolute():
            return None
        resolved = start.resolve()
        if not resolved.is_dir():
            return None
        from bounded_loops.workspace import discover

        workspace = discover(start=resolved)
    except Exception:  # noqa: BLE001 - fail-open: a hook must never strand the session
        return None
    return workspace.project_root if workspace.exists() else None


def _blocking_enabled(project_root: Path) -> bool:
    """Whether this workspace wants an active run to BLOCK the stop, or merely warn.

    Installing bounded-loops must never silently take away someone's ability to end a session.
    Blocking is the default because it is the product's own thesis pointed at the orchestrator —
    but it is a behaviour change in the user's editor, so it has an off switch that lives in
    their project rather than in our plugin files:

        [hooks]
        stop_on_active_run = false

    Turned off, the hook still reports what is active; it just does not deny. Any failure to read
    the config keeps the default, because a malformed config must not quietly disable a guard.
    """
    try:
        from bounded_loops.workspace import Workspace, read_config

        config = read_config(Workspace(project_root=project_root, origin="existing"))
    except Exception:  # noqa: BLE001 - an unreadable config keeps the default
        return True
    hooks = config.get("hooks")
    if isinstance(hooks, dict) and hooks.get("stop_on_active_run") is False:
        return False
    return True


def _allow_loudly(why: str) -> int:
    """Fail open, but SAY SO.

    Every allow-path below is a case where the hook could not perform its check. Returning 0
    silently is what makes a guard rot: the user keeps believing they are protected while nothing
    is being verified. Failing open is still the right call — a hook bug must never strand
    someone mid-session — so the cost is paid in one line of stderr instead of in false trust.
    """
    print(f"bounded-loops: run check skipped — {why}", file=sys.stderr)
    return 0


def main(argv: list[str]) -> int:
    tool = argv[1] if len(argv) > 1 else "claude-code"
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, ValueError):
        return _allow_loudly("the host sent a payload this hook could not parse")
    if not isinstance(payload, dict):
        return _allow_loudly("the host's payload was not a JSON object")

    cwd_str = _extract_cwd(payload, tool)
    if cwd_str is None:
        return _allow_loudly(f"the {tool} payload carried no working directory")

    project_root = _discover_project_root(cwd_str)
    if project_root is None:
        # Not an error and not worth a line of noise: most directories are not bounded-loops
        # workspaces, and there is genuinely nothing to check.
        return 0

    passed, reason = _check_workspace(project_root)

    if not passed and not _blocking_enabled(project_root):
        print(f"bounded-loops: {reason}", file=sys.stderr)
        return 0

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
