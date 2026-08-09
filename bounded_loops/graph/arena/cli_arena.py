"""`bl graph arena` — render a persisted run into a self-contained Arena page.

Lives in the arena package (keeps cli_graph.py within its size budget). Uses
lazy imports inside the handler to avoid an import cycle with cli_graph, which
registers this command.
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from pathlib import Path


def _err(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)


def cmd_graph_arena(args: argparse.Namespace) -> int:
    """Render one persisted run into a read-only, self-contained HTML page."""
    from bounded_loops.graph.adapters.persistence.event_log import GraphEventLog
    from bounded_loops.graph.application.arena_projection import (
        ArenaReadRequest,
        read_arena_projection,
    )
    from bounded_loops.graph.arena.render import render_arena_html
    from bounded_loops.graph.cli_graph import (
        _NoOpReceiptVerifier,
        _TrivialAuthorizer,
        _load_plan_from_run_dir,
    )
    from bounded_loops.graph.domain.errors import GraphValidationError

    run_dir = Path(args.run)
    try:
        plan, identity, run_meta = _load_plan_from_run_dir(run_dir)
    except (FileNotFoundError, ValueError, GraphValidationError) as exc:
        _err(f"graph arena: {exc}")
        return 2

    event_log = GraphEventLog(run_dir / "controller-events.jsonl", identity)
    request = ArenaReadRequest(
        subject_id=identity.organization_id,
        organization_id=identity.organization_id,
        project_id=identity.project_id,
        run_id=identity.run_id,
    )
    try:
        projection = read_arena_projection(
            plan, event_log, request, _TrivialAuthorizer(), _NoOpReceiptVerifier(),
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as a clean CLI error
        _err(f"graph arena: arena projection failed — {exc}")
        return 2

    payload = dataclasses.asdict(projection)
    payload["demonstration"] = bool(run_meta.get("demonstration"))
    payload["verified"] = False  # local render, not a signed attestation

    out_path = Path(args.out) if getattr(args, "out", None) else (run_dir / "arena.html")
    if out_path.is_symlink():
        _err(f"graph arena: output path '{out_path}' is a symlink; aborting")
        return 2
    out_path.write_text(render_arena_html(payload), encoding="utf-8")
    print(f"Arena written to {out_path}")
    print("Open it in any browser — it is read-only and needs no network.")
    return 0
