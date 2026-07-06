"""
StubRunner — replays a JSON cassette of pre-recorded agent turns.

 An earlier cassette draft only wrote a log file
and never mutated the workspace, so the flagship demo could never reach a
green gate. This version carries an explicit `actions` array per
interaction and actually applies `write_file`/`noop` actions to
`ctx.workspace` — the workspace really changes, so a downstream gate has
something true to check.

Invariants:
  - NEVER calls a gate or shell subprocess.
  - NEVER makes network calls.
  - NEVER modifies the cassette file.
  - NEVER writes outside ctx.workspace.
  - run_once is idempotent for a given lap.
  - The cassette is parsed exactly once, in __init__. Lap-level errors are
    RunnerError, not ValueError/IndexError — the runner IS the error
    boundary for callers.
"""

from __future__ import annotations

import fnmatch
import json
from pathlib import Path
from typing import Any, Optional, Union

from bounded_loops.domain.errors import RunnerError
from bounded_loops.domain.models import LoopContext, RunResult, Spec

_KNOWN_ACTION_TYPES = ("write_file", "noop")


def _is_forbidden_write(rel_path: str, forbid: tuple[str, ...]) -> bool:
    """
    True if a workspace-relative write path matches any Spec.forbid entry
    interpreted as a filename glob (fnmatch), against either the full
    POSIX-relative path or its basename.

    Hardening: a loop-author-controlled cassette must not be able
    to write the gate's own verification anchor (e.g. seed/test_slugify.py)
    with a tautological `assert True` and thereby make the gate pass on a
    tampered test — the exact "agent talked its way past the gate" failure
    the whole project claims is impossible. A loop declares its protected
    paths as glob patterns in `forbid:` (e.g. ["seed/test_*.py"]). Free-text
    forbid entries meant as prompt instructions (containing spaces) simply
    never match a real path, so they remain harmless no-ops here.
    """
    # Case-INSENSITIVE: on a case-insensitive
    # filesystem (macOS APFS) a cassette path `Seed/test_x.py` resolves to the
    # same file as `seed/test_x.py` but a case-sensitive fnmatch would let it
    # dodge the `seed/test_*.py` glob. Match lowercased. (The engine-level
    # AnchorGuardRunner also enforces this for every runner as defense-in-depth.)
    rel_low = rel_path.lower()
    base_low = rel_path.rsplit("/", 1)[-1].lower()
    for pattern in forbid:
        pat = pattern.lower()
        if fnmatch.fnmatch(rel_low, pat) or fnmatch.fnmatch(base_low, pat):
            return True
    return False


class StubRunner:
    """Replays a JSON cassette; applies its actions for real."""

    cassette_path: Path
    _by_lap: dict[int, dict[str, Any]]
    _catchall: Optional[dict[str, Any]]

    def __init__(self, cassette_path: Path) -> None:
        if not cassette_path.exists():
            raise RunnerError(f"Cassette not found: {cassette_path}")

        # The runner IS the error boundary (see module docstring): a
        # corrupted/truncated/non-object cassette must surface as RunnerError,
        # never a raw JSONDecodeError/AttributeError escaping wire() into an
        # unhandled traceback (it would otherwise crash the CLI with exit 1
        # instead of the documented wiring-error path, and crash the MCP
        # server process outright).
        try:
            raw = json.loads(cassette_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise RunnerError(
                f"Cassette {cassette_path} could not be read as valid JSON: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise RunnerError(
                f"Cassette {cassette_path} must be a JSON object, got "
                f"{type(raw).__name__}."
            )

        if raw.get("version") != 1:
            raise RunnerError(
                f"Unsupported cassette version {raw.get('version')!r} in "
                f"{cassette_path}. Only version=1 is supported."
            )

        interactions = raw.get("interactions")
        if not isinstance(interactions, list) or len(interactions) == 0:
            raise RunnerError(f"Cassette has no interactions: {cassette_path}")

        by_lap: dict[int, dict[str, Any]] = {}
        catchall: Optional[dict[str, Any]] = None
        expected_next = 1

        for idx, entry in enumerate(interactions):
            lap_field: Union[int, str, None] = entry.get("lap")
            if lap_field == "*":
                if catchall is not None:
                    raise RunnerError(
                        f"Cassette has more than one '*' catch-all entry: "
                        f"{cassette_path}"
                    )
                catchall = entry
                continue
            if lap_field != expected_next:
                raise RunnerError(
                    f"Cassette lap sequence broken at index {idx}: "
                    f"expected lap={expected_next}, got lap={lap_field!r}"
                )
            by_lap[expected_next] = entry
            expected_next += 1

        all_entries = list(by_lap.values()) + ([catchall] if catchall else [])
        for entry in all_entries:
            for action in entry.get("actions", []):
                if action.get("type") not in _KNOWN_ACTION_TYPES:
                    raise RunnerError(
                        f"Unknown cassette action type: {action.get('type')!r}"
                    )

        self._by_lap = by_lap
        self._catchall = catchall
        self.cassette_path = cassette_path

    def run_once(self, spec: Spec, ctx: LoopContext) -> RunResult:
        lap = ctx.lap
        entry = self._by_lap.get(lap) or self._catchall
        if entry is None:
            raise RunnerError(
                f"StubRunner: cassette '{self.cassette_path.name}' has no "
                f"entry for lap={lap} and no '*' catch-all. Add more "
                "interactions to the cassette."
            )

        # Log text — kept for gate/debug inspection, does NOT itself
        # mutate anything.
        output_file = ctx.workspace / "agent_output.txt"
        output_file.write_text(entry["agent_output"], encoding="utf-8")

        # Apply actions — THIS is what makes the workspace change
        #.
        for action in entry.get("actions", []):
            if action["type"] == "noop":
                continue
            if action["type"] == "write_file":
                target = (ctx.workspace / action["path"]).resolve()
                workspace_resolved = ctx.workspace.resolve()
                # Security fix: reject any path escaping the
                # workspace, e.g. a malicious cassette with
                # path="../../.ssh/id_rsa".
                if not target.is_relative_to(workspace_resolved):
                    raise RunnerError(
                        f"StubRunner: cassette action path "
                        f"{action['path']!r} resolves outside the "
                        f"workspace ({target} not inside "
                        f"{workspace_resolved}) — rejected."
                    )
                # Security fix: refuse writes to any path the loop
                # declared forbidden (e.g. the gate's own test anchor). A
                # community cassette cannot overwrite seed/test_*.py with a
                # tautology to fake a green gate.
                rel_posix = target.relative_to(workspace_resolved).as_posix()
                if _is_forbidden_write(rel_posix, spec.forbid):
                    raise RunnerError(
                        f"StubRunner: cassette action writes {action['path']!r}, "
                        f"which matches a forbidden path in the loop's spec "
                        f"(forbid={list(spec.forbid)!r}) — rejected. A cassette "
                        f"may not overwrite the gate's verification anchor."
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(action["content"], encoding="utf-8")

        return RunResult(
            changed=bool(entry["changed"]),
            agent_claimed_done=bool(entry["agent_claimed_done"]),
            tokens=int(entry.get("tokens", 0)),
            log=entry["agent_output"],
        )
