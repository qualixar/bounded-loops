"""`bl graph arena` — render a persisted run into a self-contained Arena page.

Lives in the arena package (keeps cli_graph.py within its size budget). Uses
lazy imports inside the handler to avoid an import cycle with cli_graph, which
registers this command.

Cross-model audit (C-075 read path): when the run directory contains an
``audit-plan.json``, the command computes ``read_audit_projection`` and adds the
coverage cells + release decision to the Arena payload.  If the file is absent
the Arena renders EXACTLY as before (backward compatible).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
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
    from bounded_loops.graph.application.plan_persistence import load_plan_from_run_dir
    from bounded_loops.graph.cli_graph import (
        _NoOpReceiptVerifier,
        _TrivialAuthorizer,
    )
    from bounded_loops.graph.domain.errors import GraphValidationError

    run_dir = Path(args.run)
    try:
        plan, identity, run_meta = load_plan_from_run_dir(run_dir)
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

    # Cross-model audit coverage (read-side, additive, backward-compatible).
    # When audit-plan.json is absent the payload is unchanged and the section
    # is not rendered — existing Arena renders are byte-identical to before.
    audit_plan_path = run_dir / "audit-plan.json"
    if audit_plan_path.is_file() and not audit_plan_path.is_symlink():
        try:
            from bounded_loops.graph.adapters.persistence.artifact_store import LocalArtifactStore
            from bounded_loops.graph.adapters.persistence.audit_store import plan_from_mapping
            from bounded_loops.graph.application.audit_projection import read_audit_projection

            audit_plan_raw = json.loads(audit_plan_path.read_text(encoding="utf-8"))
            audit_plan = plan_from_mapping(audit_plan_raw)
            artifact_store = LocalArtifactStore(run_dir / "artifacts")
            coverage = read_audit_projection(
                plan=plan,
                event_log=event_log,
                artifact_store=artifact_store,
                audit_plan=audit_plan,
                organization_id=identity.organization_id,
                project_id=identity.project_id,
            )
            payload["audit_coverage"] = dataclasses.asdict(coverage)
        except Exception as exc:  # noqa: BLE001
            # FAIL CLOSED: audit-plan.json is present, so the product intent is "show a release gate".
            # A projection failure (corrupt plan, unreadable store, any unexpected error) must render
            # as a BLOCKED audit section — never silently vanish, which would look identical to a run
            # with NO plan and is a fail-OPEN presentation of a release control (C-079 BLOCKER B1/F-03).
            _err(f"graph arena: audit projection failed closed (release blocked) — {exc}")
            payload["audit_coverage"] = {
                "cells": [],
                "released": False,
                "reason": f"audit plan present but projection failed closed: {exc}",
                "blocking_cells": ["*"],
                "notes": [f"projection_error: {exc}"],
            }

    out_path = Path(args.out) if getattr(args, "out", None) else (run_dir / "arena.html")
    if out_path.is_symlink():
        _err(f"graph arena: output path '{out_path}' is a symlink; aborting")
        return 2
    out_path.write_text(render_arena_html(payload), encoding="utf-8")
    print(f"Arena written to {out_path}")
    print("Open it in any browser — it is read-only and needs no network.")
    return 0
