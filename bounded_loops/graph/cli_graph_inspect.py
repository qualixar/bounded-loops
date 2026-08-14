"""``bl graph lint`` / ``plan`` / ``status`` — the read-only inspection commands.

Split out of ``cli_graph.py`` to keep it under the 800-line cap. These three share one property
that makes them a coherent module: none of them runs a node or spends anything. ``cli_graph.py``
keeps the commands that do.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

from bounded_loops.graph.adapters.persistence.event_log import GraphEventLog
from bounded_loops.graph.adapters.persistence.local_arena_access import (
    LocalSameTenantAuthorizer,
    UnverifiedReceiptReader,
)
from bounded_loops.graph.application.arena_projection import (
    ArenaReadRequest,
    read_arena_projection,
)
from bounded_loops.graph.application.compile_graph import CompileSnapshot, compile_graph
from bounded_loops.graph.application.validate_graph import (
    parse_authoring_graph_json,
    parse_authoring_graph_yaml,
)
from bounded_loops.graph.domain.authoring import _NULL_POLICY_DIGEST
from bounded_loops.graph.application.plan_persistence import load_plan_from_run_dir
from bounded_loops.graph.domain.errors import GraphValidationError


def _err(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)


def cmd_graph_lint(args: argparse.Namespace) -> int:
    """bl graph lint <manifest.(yaml|json)> — validate; print digest + counts."""
    manifest_path = Path(args.manifest)
    suffix = manifest_path.suffix.lower()
    # User-supplied path: symlinks intentionally allowed (local CLI, like `cat`).
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        _err(f"graph lint: cannot read '{manifest_path}' — {exc}")
        if getattr(args, "json", False):
            print(json.dumps({"valid": False, "code": "io_error",
                              "pointer": "/", "message": str(exc)},
                             sort_keys=True))
        return 2

    try:
        if suffix == ".json":
            spec = parse_authoring_graph_json(text)
        elif suffix in (".yaml", ".yml"):
            spec = parse_authoring_graph_yaml(text)
        else:
            msg = f"graph lint: unsupported extension '{suffix}'; expected .yaml or .json"
            _err(msg)
            if getattr(args, "json", False):
                print(json.dumps({"valid": False, "code": "unsupported_extension",
                                  "pointer": "/", "message": msg},
                                 sort_keys=True))
            return 2
    except GraphValidationError as exc:
        _err(f"graph lint: [{exc.code}] {exc.pointer} — {exc.message}")
        if getattr(args, "json", False):
            print(json.dumps(
                {"valid": False, "code": exc.code,
                 "pointer": exc.pointer, "message": exc.message},
                sort_keys=True,
            ))
        return 2

    node_ids = [n.id for n in spec.nodes]
    slot_ids = [s.id for s in spec.connection_slots]

    if getattr(args, "json", False):
        print(json.dumps(
            {
                "digest": spec.digest,
                "edge_count": len(spec.edges),
                "node_ids": node_ids,
                "schema_version": 1,
                "slot_ids": slot_ids,
                "valid": True,
            },
            sort_keys=True,
        ))
    else:
        print(f"digest  : {spec.digest}")
        print(f"nodes   : {len(node_ids)} ({', '.join(node_ids)})")
        print(f"edges   : {len(spec.edges)}")
        print(f"slots   : {len(slot_ids)} ({', '.join(slot_ids)})")
        print("OK")
    return 0


def cmd_graph_plan(args: argparse.Namespace) -> int:
    """bl graph plan <manifest> [--connections <json>] — validate then compile."""
    manifest_path = Path(args.manifest)
    suffix = manifest_path.suffix.lower()
    # User-supplied path: symlinks intentionally allowed (local CLI, like `cat`).
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        _err(f"graph plan: cannot read '{manifest_path}' — {exc}")
        return 2

    try:
        if suffix == ".json":
            graph = parse_authoring_graph_json(text)
        elif suffix in (".yaml", ".yml"):
            graph = parse_authoring_graph_yaml(text)
        else:
            _err(f"graph plan: unsupported extension '{suffix}'")
            return 2
    except GraphValidationError as exc:
        _err(f"graph plan: validation failed [{exc.code}] {exc.pointer} — {exc.message}")
        return 2

    if graph.connection_slots and not getattr(args, "connections", None):
        _err("graph plan: compile requires --connections for connection-bound nodes")
        return 2

    connections_raw: list[object] = []
    if getattr(args, "connections", None):
        try:
            connections_raw = json.loads(
                Path(args.connections).read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            _err(f"graph plan: cannot load connections — {exc}")
            return 2

    try:
        snapshot = CompileSnapshot(
            policy_digest=_NULL_POLICY_DIGEST,
            package_digests=frozenset(),
            connections=tuple(connections_raw),  # type: ignore[arg-type]
        )
        plan = compile_graph(graph, snapshot)
    except GraphValidationError as exc:
        _err(f"graph plan: compile failed [{exc.code}] {exc.pointer} — {exc.message}")
        return 2

    if getattr(args, "json", False):
        nodes_out = [
            {
                "binding_id": n.binding_id,
                "effects": sorted(e.value for e in n.required_effects),
                "isolation": n.isolation.value,
                "kind": n.kind,
                "node_id": n.node_id,
            }
            for n in plan.nodes
        ]
        bindings_out = [
            {
                "binding_id": b.binding_id,
                "provider_id": b.provider_id,
                "transport": b.transport,
            }
            for b in plan.connection_bindings
        ]
        print(json.dumps(
            {
                "bindings": bindings_out,
                "levels": [list(level) for level in plan.levels],
                "nodes": nodes_out,
                "plan_id": plan.plan_id,
                "policy_digest": plan.policy_digest,
                "schema_version": 1,
                "source_graph_digest": plan.source_graph_digest,
            },
            sort_keys=True,
        ))
    else:
        print(f"plan_id : {plan.plan_id}")
        print(f"graph   : {plan.source_graph_digest}")
        print(f"policy  : {plan.policy_digest}")
        for i, level in enumerate(plan.levels):
            nodes_in_level = ", ".join(level)
            print(f"wave {i}  : [{nodes_in_level}]")
        for node in plan.nodes:
            effects = ", ".join(sorted(e.value for e in node.required_effects))
            print(
                f"  node {node.node_id!r}: kind={node.kind}  "
                f"effects=[{effects}]  isolation={node.isolation.value}"
            )
    return 0


_STATUS_NOTICE = "LOCAL/UNVERIFIED — status is read from a local event log; not verified by an Arena server."


def cmd_graph_status(args: argparse.Namespace) -> int:
    """bl graph status --run <dir> — reconstruct plan + read arena projection."""
    run_dir = Path(args.run)
    if not run_dir.is_dir():
        _err(f"graph status: '{run_dir}' is not a directory")
        return 2

    try:
        plan, identity, run_meta = load_plan_from_run_dir(run_dir)
    except (FileNotFoundError, ValueError, GraphValidationError) as exc:
        _err(f"graph status: cannot reconstruct plan — {exc}")
        return 2

    try:
        event_log = GraphEventLog(run_dir / "controller-events.jsonl", identity)
    except Exception as exc:  # noqa: BLE001
        _err(f"graph status: cannot open event log — {exc}")
        return 2

    request = ArenaReadRequest(
        subject_id=identity.organization_id, organization_id=identity.organization_id,
        project_id=identity.project_id, run_id=identity.run_id,
    )
    try:
        projection = read_arena_projection(
            plan,
            event_log,
            request,
            LocalSameTenantAuthorizer(),
            UnverifiedReceiptReader(),
        )
    except Exception as exc:  # noqa: BLE001
        _err(f"graph status: arena projection failed — {exc}")
        return 2

    is_demo: bool = bool(run_meta.get("demonstration"))

    if getattr(args, "json", False):
        # dataclasses.asdict on ArenaProjection — tuple fields become lists.
        out_dict = dataclasses.asdict(projection)
        out_dict["demonstration"] = is_demo
        out_dict["notice"] = _STATUS_NOTICE
        out_dict["verified"] = False
        print(json.dumps(out_dict, sort_keys=True))
    else:
        print(f"notice    : {_STATUS_NOTICE}")
        print(f"demonstration: {is_demo}")
        print(f"run_state : {projection.run_state}")
        print(f"run_id    : {projection.run_id}")
        print()
        header = f"{'NODE':<20} {'KIND':<20} {'STATE':<12} {'ISOLATION':<22} {'EFFECTS':<20} ARTIFACTS"
        print(header)
        print("-" * len(header))
        for node in projection.nodes:
            effects = ",".join(node.required_effects) or "-"
            artifacts = ",".join(node.artifact_digests[:1]) or "-"
            if artifacts != "-":
                artifacts = artifacts[:20] + "..."
            print(
                f"{node.node_id:<20} {node.kind:<20} {node.state:<12} "
                f"{node.isolation:<22} {effects:<20} {artifacts}"
            )
    return 0
