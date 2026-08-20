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


def _parse_loop_roots(raw: list[str] | None) -> tuple[Path, ...] | None:
    """Convert an argparse append-list to a tuple of Paths, or None (use defaults)."""
    return tuple(Path(p) for p in raw) if raw else None


def _read_loop_outcome(artifact_digest: str, run_dir: Path) -> dict[str, object] | None:
    """Read and parse the loop-outcome artifact from the run's artifact store."""
    hex_digest = artifact_digest.removeprefix("sha256:")
    artifact_path = run_dir / "artifacts" / "objects" / hex_digest
    if artifact_path.is_file() and not artifact_path.is_symlink():
        try:
            data = json.loads(artifact_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (json.JSONDecodeError, OSError):
            pass
    return None


def _augment_loop_nodes(
    payload: dict[str, object],
    plan: object,
    run_dir: Path,
    loop_roots: tuple[Path, ...] | None,
) -> None:
    """Add loop_meta to every kind:loop node in the payload dict.

    Fail-open: any error (unresolvable root, missing yaml, bad artifact) is
    swallowed and the field is simply absent.  The Arena template checks for
    the field before rendering it, so backward compatibility is preserved.
    """
    try:
        import yaml as _yaml  # pyyaml — already a project dependency
        from bounded_loops.graph.adapters.workers.loop_packages import (
            LoopPackageRegistry,
            normalise_package_digest,
        )
        from bounded_loops.graph.loop_node_wiring import _default_loop_roots
    except ImportError:
        return

    roots = loop_roots if loop_roots is not None else _default_loop_roots()
    try:
        index: dict[str, Path] = dict(LoopPackageRegistry(roots=roots).index())
    except Exception:  # noqa: BLE001
        index = {}

    # Build node_id -> PlannedNode map for loop nodes from the immutable plan.
    loop_planned: dict[str, object] = {
        n.node_id: n  # type: ignore[attr-defined]
        for n in plan.nodes  # type: ignore[attr-defined]
        if n.kind == "loop"  # type: ignore[attr-defined]
    }

    nodes = payload.get("nodes", [])
    if not isinstance(nodes, (list, tuple)):
        return

    for node_dict in nodes:
        if not isinstance(node_dict, dict) or node_dict.get("kind") != "loop":
            continue
        node_id = str(node_dict.get("node_id", ""))
        planned = loop_planned.get(node_id)
        if planned is None:
            continue
        package_digest: str = getattr(planned, "package_digest", None) or ""
        meta: dict[str, object] = {"package_digest": package_digest}

        # Resolve package name from the on-disk loop.yaml (fail-open).
        if package_digest:
            pkg_dir = index.get(normalise_package_digest(package_digest))
            if pkg_dir is not None:
                try:
                    cfg = _yaml.safe_load((pkg_dir / "loop.yaml").read_text(encoding="utf-8"))
                    if isinstance(cfg, dict):
                        if cfg.get("name"):
                            meta["package_name"] = str(cfg["name"])
                        if cfg.get("description"):
                            meta["package_description"] = str(cfg["description"])
                except Exception:  # noqa: BLE001
                    pass

        # Read the promoted loop-outcome artifact for ANY state that produced one, not only
        # SUCCEEDED. A failed loop node is exactly when an operator needs to know WHICH failure it
        # was — the loop halted with its gate still rejecting, or it was killed on wallclock, or it
        # never launched at all — and restricting this to SUCCEEDED dropped that at the one moment
        # it mattered. Its own review named this the most important information in incident review.
        art = node_dict.get("artifact_digests")
        if isinstance(art, (list, tuple)) and art:
            outcome = _read_loop_outcome(str(art[0]), run_dir)
            if outcome is not None:
                meta["loop_outcome"] = outcome

        # Say plainly when there is no receipt to show. Without this the badge renders identically
        # on a node whose loop ran and failed its gate and on a node whose loop NEVER STARTED, which
        # reads as "the loop ran and something went wrong" in both cases. The absence of an artifact
        # is itself evidence, and naming it is the difference between a UI and a decoration.
        if "loop_outcome" not in meta and node_dict.get("state") not in {"PENDING", "READY"}:
            meta["no_receipt_reason"] = (
                "no loop receipt was promoted for this node, so the loop did not reach the point of "
                "writing one — this is a node-level failure BEFORE the loop ran, not a loop that ran "
                "and was rejected"
            )

        node_dict["loop_meta"] = meta


def cmd_graph_arena(args: argparse.Namespace) -> int:
    """Render one persisted run into a read-only, self-contained HTML page."""
    from bounded_loops.graph.adapters.persistence.event_log import GraphEventLog
    from bounded_loops.graph.adapters.persistence.local_arena_access import (
        LocalSameTenantAuthorizer,
        UnverifiedReceiptReader,
    )
    from bounded_loops.graph.application.arena_projection import (
        ArenaReadRequest,
        read_arena_projection,
    )
    from bounded_loops.graph.arena.render import render_arena_html
    from bounded_loops.graph.application.plan_persistence import load_plan_from_run_dir
    from bounded_loops.graph.domain.errors import GraphValidationError

    run_dir = Path(args.run)
    loop_roots_early = _parse_loop_roots(getattr(args, "loop_roots", None))
    try:
        from bounded_loops.graph.loop_node_wiring import admitted_loop_package_digests
        pkg_digests: frozenset[str] = admitted_loop_package_digests(loop_roots_early)
    except Exception:  # noqa: BLE001 — fail-open; plan load will re-raise if actually missing
        pkg_digests = frozenset()
    try:
        plan, identity, run_meta = load_plan_from_run_dir(run_dir, package_digests=pkg_digests)
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
            plan, event_log, request, LocalSameTenantAuthorizer(), UnverifiedReceiptReader(),
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as a clean CLI error
        _err(f"graph arena: arena projection failed — {exc}")
        return 2

    payload = dataclasses.asdict(projection)
    payload["demonstration"] = bool(run_meta.get("demonstration"))
    payload["verified"] = False  # local render, not a signed attestation

    # Loop node evidence: augment each kind:loop node with its package name and
    # the loop-outcome artifact (fail-open, additive, backward-compatible).
    _augment_loop_nodes(payload, plan, run_dir, loop_roots_early)

    # Cross-model audit coverage (read-side, additive, backward-compatible).
    # When audit-plan.json is absent the payload is unchanged and the section
    # is not rendered — existing Arena renders are byte-identical to before.
    audit_plan_path = run_dir / "audit-plan.json"
    if audit_plan_path.is_file() and not audit_plan_path.is_symlink():
        try:
            from bounded_loops.graph.adapters.persistence.artifact_store import LocalArtifactStore
            from bounded_loops.graph.domain.audit_serde import plan_from_mapping
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
