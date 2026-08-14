"""How a finished graph run is reported to a human, and with what exit code.

Split out of ``graph_composition.py`` to keep it under the 800-line cap. The split is not merely
by size: composition decides WHAT runs, this decides what the operator is TOLD, and the two change
for entirely different reasons — a new provider transport touches the former, a clearer paused-run
message touches the latter.

Exit codes are the contract scripts depend on: ``0`` succeeded, ``2`` failed, ``3`` paused awaiting
a human. ``3`` is deliberately distinct from failure — a paused run has not failed, and a CI job
that treats it as one would discard work that is still resumable.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from bounded_loops.graph.application.arena_projection import ArenaProjection


_EXIT_PAUSED = 3


def _awaiting_approval_nodes(arena: ArenaProjection) -> tuple[str, ...]:
    """Node IDs currently paused at a human approval checkpoint, in plan order.

    A non-empty result is the authoritative signal that the run is durably paused —
    reused verbatim by `bl graph approve`'s own reporting so both commands agree on
    what "still paused" means.
    """
    return tuple(node.node_id for node in arena.nodes if node.state == "AWAITING_APPROVAL")


def approve_command_hint(out_dir: Path, node_id: str) -> str:
    """The exact next command a human runs to decide one paused approval node."""
    return f"bl graph approve --run {out_dir} --node {node_id} --decision approved|rejected"


def _report(
    json_out: bool, out_dir: Path, run_state: str, arena: ArenaProjection,
    *, mode: str = "local_cli",
) -> int:
    awaiting = _awaiting_approval_nodes(arena)
    # A run whose authoritative run_state is FAILED must never be reported as merely
    # paused, even if a node's LAST durable receipt still shows AWAITING_APPROVAL (e.g.
    # a sibling node in the same wave failed before this node's decision was
    # revisited — `_awaiting_approval_nodes` only looks at each node's latest state,
    # not the run's own terminal outcome). PAUSED implies the run is still resumable;
    # a FAILED run is not (dual-audit residual MINOR).
    if awaiting and run_state != "FAILED":
        return _report_paused(json_out, out_dir, run_state, arena, awaiting, mode=mode)
    succeeded = run_state == "SUCCEEDED"
    digests = [n.artifact_digests[0] for n in arena.nodes if n.artifact_digests]
    if json_out:
        print(json.dumps({
            "execution": True,
            "mode": mode,
            "run_state": run_state,
            "run_id": arena.run_id,
            "out": str(out_dir),
            "artifact_digests": digests,
        }, sort_keys=True))
        return 0 if succeeded else 2
    label = "BYOK/HTTP" if mode == "https" else "Local-CLI"
    print(f"{label} graph run — REAL execution")
    print("=" * 62)
    print(f"run_state : {run_state}")
    for node in arena.nodes:
        mark = "OK " if node.state == "SUCCEEDED" else "!! "
        art = (node.artifact_digests[0][:24] + "...") if node.artifact_digests else "-"
        print(f"  {mark}node {node.node_id!r}: {node.state}  artifact={art}")
    print(f"out       : {out_dir}")
    if succeeded:
        print()
        print(f"Open the visual Arena:  bl graph arena --run {out_dir}")
        return 0
    print()
    print("Run did not succeed; inspect the event log in the run directory.")
    return 2


def _report_paused(
    json_out: bool, out_dir: Path, run_state: str, arena: ArenaProjection,
    awaiting: tuple[str, ...], *, mode: str = "local_cli",
) -> int:
    """Report a run durably PAUSED at one or more human approval checkpoints.

    NOT an error: the run is exactly where it should be, waiting on a decision that
    only a human (via `bl graph approve`) can make. Exit code is `_EXIT_PAUSED` (3) —
    distinct from success (0) and failure (2) — in both the JSON and human paths.
    """
    next_commands = [approve_command_hint(out_dir, node_id) for node_id in awaiting]
    if json_out:
        print(json.dumps({
            "execution": True,
            "mode": mode,
            "run_state": run_state,
            "run_id": arena.run_id,
            "out": str(out_dir),
            "paused": True,
            "awaiting_approval": list(awaiting),
            "next_commands": next_commands,
        }, sort_keys=True))
        return _EXIT_PAUSED
    label = "BYOK/HTTP" if mode == "https" else "Local-CLI"
    print(f"{label} graph run — REAL execution")
    print("=" * 62)
    print(f"run_state : {run_state}")
    print(f"pause_status : PAUSED — awaiting human decision on "
          f"{len(awaiting)} node(s): {', '.join(awaiting)}")
    for node in arena.nodes:
        if node.state == "SUCCEEDED":
            mark = "OK "
        elif node.state == "AWAITING_APPROVAL":
            mark = "~~ "  # DX-13: ~~ = "paused/pending" (less cryptic than ??);
        else:
            mark = "!! "
        art = (node.artifact_digests[0][:24] + "...") if node.artifact_digests else "-"
        print(f"  {mark}node {node.node_id!r}: {node.state}  artifact={art}")
    print(f"out       : {out_dir}")
    print()
    print("To continue:")
    for command in next_commands:
        print(f"  {command}")
    return _EXIT_PAUSED


def _fail(json_out: bool, message: str) -> int:
    if json_out:
        print(json.dumps({"execution": False, "error": message}, sort_keys=True))
    else:
        print(f"error: {message}", file=sys.stderr)
    return 2
