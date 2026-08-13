"""Graph CLI handlers for `bl graph` — delegates to existing application use cases.

Honesty contract (never violate):
- `run`  : compile a manifest (honest preview); `--execute --out <dir>` REALLY
           runs a graph inside a native OS sandbox (no Docker), proven by an
           independent gate. With NO manifest it runs the built-in demo; with an
           admitted local-CLI or BYOK/HTTP manifest (+ --connections/--inputs/
           --admitted) it runs that graph's connector nodes for real. An
           approval-checkpoint node PAUSES the run (durably, AWAITING_APPROVAL)
           rather than being refused — sandboxed tool nodes stay refused until a
           later phase.
- `approve` : records a durable human decision (approved/rejected) for one
           paused approval node and resumes the run past it, via
           `LocalGraphRuntimeFacade.approve()` unchanged.
- `demo` : PROMINENT banner labels the run as a DEMONSTRATION with no
           sandbox, isolation, or network enforcement.

Each public cmd_graph_* function accepts an argparse.Namespace and returns int.
register(subparsers) wires all subparsers under the "graph" group.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

from bounded_loops.graph.adapters.persistence.event_log import GraphEventLog
from bounded_loops.graph.application.arena_projection import (
    ArenaReadRequest,
    read_arena_projection,
)
from bounded_loops.graph.application.compile_graph import (
    CompileSnapshot,
    compile_graph,
)
from bounded_loops.graph.application.plan_persistence import load_plan_from_run_dir
from bounded_loops.graph.application.node_spend import RunBudget
from bounded_loops.graph.domain.errors import GraphIntegrityError
from bounded_loops.graph.domain.pricing import PriceTable
from bounded_loops.graph.application.validate_graph import (
    parse_authoring_graph_json,
    parse_authoring_graph_yaml,
)
from bounded_loops.graph.domain.authoring import _NULL_POLICY_DIGEST
from bounded_loops.graph.domain.errors import GraphValidationError
from bounded_loops.graph.domain.events import GraphRunIdentity
# cmd_graph_artifacts, cmd_graph_approve, and cmd_graph_demo live in sibling modules to
# keep this file within the 800-line hard cap; re-exported here so `bl graph artifacts`,
# `bl graph approve`, `bl graph demo`, and any existing imports resolve unchanged.
# DEMO_MANIFEST_YAML and DEMO_CONNECTIONS_LIST are re-exported for the same reason.
from bounded_loops.graph.cli_graph_artifacts import cmd_graph_artifacts
from bounded_loops.graph.cli_graph_approve import cmd_graph_approve
from bounded_loops.graph.cli_graph_demo import (
    DEMO_CONNECTIONS_LIST,  # noqa: F401 — re-exported for test backward-compat (ARCH-05)
    DEMO_MANIFEST_YAML,  # noqa: F401 — re-exported for test backward-compat (ARCH-05)
    cmd_graph_demo,
)

# Identity for a `bl graph run --execute <manifest>` run. These are fixed identity
# values for this CLI's single-tenant entry point — they shape the plan/event-log/
# approval-ledger derivation (via GraphRunIdentity), NOT the physical directory layout.
# `_execute_manifest` writes FLAT, directly into `--out <dir>` (0.4.0 — dual-audit
# reconciliation, design Q4/M2): earlier in 0.4.0-beta this nested the run three levels
# under `<dir>/<org>/<project>/<run_id>/` purely so `bl graph approve` could satisfy
# `LocalGraphRuntimeFacade`'s hosted `runs_root/organization_id/project_id/run_id`
# addressing convention; both the Grok and Muse adversarial audits flagged that as MAJOR
# public-contract debt (it silently changed `--out`'s meaning and only existed to reuse
# that path math). `bl graph approve` now opens `--out <dir>` literally via
# `LocalGraphRuntimeFacade.for_run_dir`, so the nesting is gone.
_CLI_EXECUTE_ORG = "local-org"
_CLI_EXECUTE_PROJECT = "local-project"
_CLI_EXECUTE_RUN_ID = "graph-run"


class _TrivialAuthorizer:
    """Always authorises; used for local same-tenant arena reads."""

    def authorize(self, request: ArenaReadRequest) -> bool:
        return True


class _NoOpReceiptVerifier:
    """No-op receipt verifier for local reads."""

    def verify(self, identity: GraphRunIdentity, receipts: object) -> None:
        pass


# ── private helpers ────────────────────────────────────────────────────────────

def _err(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)


# ── handlers ───────────────────────────────────────────────────────────────────

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
            _TrivialAuthorizer(),
            _NoOpReceiptVerifier(),
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


def _resolve_budget(args: argparse.Namespace) -> tuple["RunBudget", "PriceTable"]:
    """The run's spend ceilings and the rates that price them, file first then flags.

    A file holds the deployment's standing numbers; a flag is typed deliberately for the run
    in front of you, so a flag wins. Overriding happens per dimension: setting --max-tokens
    says nothing about a cost ceiling in the file, and silently dropping that would remove a
    bound the operator still expects to hold.
    """
    from bounded_loops.graph.application.budget_config import (
        load_budget_file,
        resolve_run_budget,
    )
    from bounded_loops.graph.domain.pricing import empty_price_table

    from_file, table = RunBudget(), empty_price_table()
    if getattr(args, "budget_file", None):
        from_file, table = load_budget_file(Path(args.budget_file))
    budget = resolve_run_budget(
        from_file=from_file,
        max_tokens=getattr(args, "max_tokens", None),
        max_cost_usd=getattr(args, "max_cost_usd", None),
    )
    return budget, table


def _execute_manifest(args: argparse.Namespace, manifest: str, out_dir: Path) -> int:
    """Read a user manifest (+ optional --connections/--inputs/--admitted) and run it for real.

    The run is written FLAT, directly into ``out_dir`` (0.4.0 flat addressing) — every
    reported "out" path (human text, ``--json``) is exactly ``out_dir``, so a caller who
    copies it verbatim into a later ``bl graph status/arena/artifacts/approve --run
    <path>`` always gets the right directory, with no nesting to account for.
    """
    manifest_path = Path(manifest)
    suffix = manifest_path.suffix.lower()
    if suffix not in (".json", ".yaml", ".yml"):
        _err(f"graph run: unsupported extension '{suffix}'")
        return 2
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        _err(f"graph run: cannot read '{manifest_path}' — {exc}")
        return 2
    connections_raw: list[object] = []
    if getattr(args, "connections", None):
        try:
            connections_raw = json.loads(Path(args.connections).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _err(f"graph run: cannot load connections — {exc}")
            return 2
    node_prompts: dict[str, str] = {}
    if getattr(args, "inputs", None):
        if Path(args.inputs).is_symlink():
            _err("graph run: --inputs must not be a symlink")
            return 2
        try:
            raw = json.loads(Path(args.inputs).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _err(f"graph run: cannot load inputs — {exc}")
            return 2
        if not isinstance(raw, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in raw.items()
        ):
            _err("graph run: --inputs must be a JSON object mapping node_id -> prompt string")
            return 2
        node_prompts = raw
    # BYOK/HTTP mode: --admitted is a JSON file containing a map of
    # connection_id -> admitted-connection-record dict.  No secrets — only env-var names.
    admitted_connections = None
    if getattr(args, "admitted", None):
        try:
            admitted_raw = json.loads(Path(args.admitted).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _err(f"graph run: cannot load --admitted records — {exc}")
            return 2
        if not isinstance(admitted_raw, dict):
            _err("graph run: --admitted must be a JSON object mapping connection_id -> record")
            return 2
        from bounded_loops.graph.adapters.connectors.admitted_connection_request import (
            AdmittedConnectionRecord,
        )
        from bounded_loops.graph.domain.errors import GraphValidationError as _GVE
        try:
            admitted_connections = {}
            for conn_id, record_raw in admitted_raw.items():
                record = AdmittedConnectionRecord.from_mapping(record_raw)
                if record.connection_id != conn_id:
                    _err(
                        f"graph run: --admitted map key {conn_id!r} does not match the record's "
                        f"connection_id {record.connection_id!r}"
                    )
                    return 2
                admitted_connections[conn_id] = record
        except _GVE as exc:
            _err(f"graph run: invalid --admitted record — [{exc.code}] {exc.pointer}: {exc.message}")
            return 2
    # Optional audit plan — read text from file, pass verbatim to execute_graph_run
    # which persists it as audit-plan.json (read-side only; does not affect the run).
    audit_plan_json_text: str | None = None
    if getattr(args, "audit_plan", None):
        try:
            audit_plan_json_text = Path(args.audit_plan).read_text(encoding="utf-8")
        except OSError as exc:
            _err(f"graph run: cannot load --audit-plan — {exc}")
            return 2

    try:
        run_budget, price_table = _resolve_budget(args)
    except GraphIntegrityError as exc:
        _err(f"graph run: {exc}")
        return 2

    from bounded_loops.graph.graph_composition import execute_graph_run
    return execute_graph_run(
        manifest_text=text,
        manifest_suffix=".json" if suffix == ".json" else ".yaml",
        connections_raw=list(connections_raw),
        node_prompts=node_prompts,
        out_dir=out_dir,
        organization_id=_CLI_EXECUTE_ORG,
        project_id=_CLI_EXECUTE_PROJECT,
        run_id=_CLI_EXECUTE_RUN_ID,
        json_out=getattr(args, "json", False),
        admitted_connections=admitted_connections,
        audit_plan_json=audit_plan_json_text,
        run_budget=run_budget,
        price_table=price_table,
    )


def cmd_graph_run(args: argparse.Namespace) -> int:
    """bl graph run — compile a manifest (honest preview), or `--execute --out
    <dir>` to REALLY run a graph inside a native OS sandbox (no Docker). With no
    manifest this runs the built-in demo; with an admitted local-CLI manifest it
    runs that graph's agent-CLI nodes for real."""
    if getattr(args, "execute", False):
        out = getattr(args, "out", None)
        if not out:
            _err("graph run --execute requires --out <dir>")
            return 2
        manifest = getattr(args, "manifest", None)
        if not manifest:
            # No manifest → the built-in native-sandbox demonstration (unchanged).
            from bounded_loops.graph.sandbox_demo import run_sandbox_demo
            return run_sandbox_demo(Path(out), json_out=getattr(args, "json", False))
        # A user manifest → REAL execution of its admitted local-CLI connector nodes.
        return _execute_manifest(args, manifest, Path(out))

    if not getattr(args, "manifest", None):
        _err("graph run: provide a <manifest>, or use --execute --out <dir> to run the built-in sandboxed demo")
        return 2

    manifest_path = Path(args.manifest)
    suffix = manifest_path.suffix.lower()
    # User-supplied path: symlinks intentionally allowed (local CLI, like `cat`).
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        _err(f"graph run: cannot read '{manifest_path}' — {exc}")
        return 2

    try:
        if suffix == ".json":
            graph = parse_authoring_graph_json(text)
        elif suffix in (".yaml", ".yml"):
            graph = parse_authoring_graph_yaml(text)
        else:
            _err(f"graph run: unsupported extension '{suffix}'")
            return 2
    except GraphValidationError as exc:
        _err(f"graph run: validation failed [{exc.code}] {exc.pointer} — {exc.message}")
        return 2

    if graph.connection_slots and not getattr(args, "connections", None):
        _err("graph run: --connections required for connection-bound nodes")
        return 2

    connections_raw: list[object] = []
    if getattr(args, "connections", None):
        try:
            connections_raw = json.loads(
                Path(args.connections).read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            _err(f"graph run: cannot load connections — {exc}")
            return 2

    try:
        snapshot = CompileSnapshot(
            policy_digest=_NULL_POLICY_DIGEST,
            package_digests=frozenset(),
            connections=tuple(connections_raw),  # type: ignore[arg-type]
        )
        plan = compile_graph(graph, snapshot)
    except GraphValidationError as exc:
        _err(f"graph run: compile failed [{exc.code}] {exc.pointer} — {exc.message}")
        return 2

    _RUN_NOTICE = "compile-only preview; use --execute to run an admitted local-CLI graph"

    if getattr(args, "json", False):
        print(json.dumps(
            {
                "levels": [list(lvl) for lvl in plan.levels],
                "nodes": [{"kind": n.kind, "node_id": n.node_id} for n in plan.nodes],
                "notice": _RUN_NOTICE,
                "plan_id": plan.plan_id,
                "schema_version": 1,
                "source_graph_digest": plan.source_graph_digest,
            },
            sort_keys=True,
        ))
        return 0

    print(f"plan_id : {plan.plan_id}")
    for i, level in enumerate(plan.levels):
        print(f"wave {i}  : [{', '.join(level)}]")
    for node in plan.nodes:
        effects = ", ".join(sorted(e.value for e in node.required_effects))
        print(
            f"  node {node.node_id!r}: kind={node.kind}  "
            f"effects=[{effects}]  isolation={node.isolation.value}"
        )
    print()
    print(
        "This is a compile-only preview; no node was executed. To really run an\n"
        "admitted local-CLI graph:  bl graph run --execute <manifest> --connections\n"
        "<json> --inputs <json> --out <dir>  (or `bl graph demo` for the pipeline)."
    )
    return 0


# ── parser registration ────────────────────────────────────────────────────────

def _cmd_graph_no_sub(args: argparse.Namespace) -> int:
    """Fallback when `bl graph` is typed without a subcommand."""
    _err("graph: missing subcommand; use `bl graph --help`")
    return 1


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Add the `graph` subcommand group to *subparsers*."""
    graph_parser = subparsers.add_parser(
        "graph",
        help="Validate, compile, and demonstrate graph execution plans.",
        description=(
            "Run a DAG of bounded loops where an independent gate decides each node. "
            "Set up with init; author with lint, plan and studio; execute with run --execute; "
            "decide a paused approval with approve or console; inspect with status, artifacts "
            "and arena; try demo first. Executing a real graph needs OS-level isolation "
            "enforcement (E2) — see docs/graph-capabilities.md."
        ),
    )
    graph_parser.set_defaults(func=_cmd_graph_no_sub)

    graph_subs = graph_parser.add_subparsers(dest="graph_cmd", metavar="ACTION")

    # lint
    lint_p = graph_subs.add_parser(
        "lint",
        help="Parse and validate a graph manifest (.yaml or .json).",
    )
    lint_p.add_argument("manifest", metavar="<manifest.(yaml|json)>",
                        help="Path to the graph manifest file.")
    lint_p.add_argument("--json", action="store_true", help="Emit JSON output.")
    lint_p.set_defaults(func=cmd_graph_lint)

    # plan
    plan_p = graph_subs.add_parser(
        "plan",
        help="Validate then compile a graph to an execution plan.",
    )
    plan_p.add_argument("manifest", metavar="<manifest.(yaml|json)>",
                        help="Path to the graph manifest file.")
    plan_p.add_argument("--connections", default=None, metavar="<json>",
                        help="Path to a JSON file containing connection candidates.")
    plan_p.add_argument("--json", action="store_true", help="Emit JSON output.")
    plan_p.set_defaults(func=cmd_graph_plan)

    # demo
    demo_p = graph_subs.add_parser(
        "demo",
        help="Run the built-in example with in-process DEMONSTRATION collaborators.",
        description="DEMONSTRATION: no sandbox / isolation / E2. Not for production.",
    )
    demo_p.add_argument("--out", required=True, metavar="<dir>",
                        help="Directory to write plan.json, event log, and artifacts.")
    demo_p.add_argument("--json", action="store_true", help="Emit JSON output.")
    demo_p.set_defaults(func=cmd_graph_demo)

    # status
    status_p = graph_subs.add_parser(
        "status",
        help="Read arena projection from a persisted run directory.",
    )
    status_p.add_argument("--run", required=True, metavar="<dir>",
                          help="Directory written by `bl graph demo`.")
    status_p.add_argument("--json", action="store_true", help="Emit JSON output.")
    status_p.set_defaults(func=cmd_graph_status)

    # artifacts
    artifacts_p = graph_subs.add_parser(
        "artifacts",
        help="List artifacts produced by a persisted run.",
    )
    artifacts_p.add_argument("--run", required=True, metavar="<dir>",
                             help="Directory written by `bl graph demo`.")
    artifacts_p.add_argument("--json", action="store_true", help="Emit JSON output.")
    artifacts_p.set_defaults(func=cmd_graph_artifacts)

    # run
    run_p = graph_subs.add_parser(
        "run",
        help="Compile a graph (preview), or --execute it (built-in demo, or an admitted local-CLI manifest) in a native sandbox.",
        epilog=(
            "Exit codes: 0 success, 2 refused or failed, 3 PAUSED — the run is durably\n"
            "waiting on a human approval-checkpoint decision. Exit code 3 is NOT an error:\n"
            "scripts using `set -e`, or any check of the form `$? -ne 0`, MUST handle it\n"
            "explicitly, e.g.:\n\n"
            "  bl graph run --execute <manifest> --out <dir>; rc=$?\n"
            "  if [ \"$rc\" = 3 ]; then bl graph approve --run <dir> --node <id> \\\n"
            "      --decision approved; fi\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    run_p.add_argument("manifest", nargs="?", default=None, metavar="<manifest.(yaml|json)>",
                       help="Path to the graph manifest file (omit with --execute for the built-in demo).")
    run_p.add_argument("--connections", default=None, metavar="<json>",
                       help="Path to a JSON file containing connection candidates.")
    run_p.add_argument("--inputs", default=None, metavar="<json>",
                       help="JSON object mapping node_id -> prompt (run-time input for local-CLI nodes).")
    run_p.add_argument("--execute", action="store_true",
                       help="Actually execute: the built-in demo (no manifest), or an admitted connector manifest.")
    run_p.add_argument("--out", default=None, metavar="<dir>",
                       help="Output run directory for --execute.")
    run_p.add_argument(
        "--admitted", default=None, metavar="<json>",
        help=(
            "Path to a JSON file containing a map of connection_id -> admitted-connection "
            "record (BYOK/HTTP mode).  Each record names the endpoint, the credential "
            "ENV-VAR name (never the value), the expiry, and the request style.  "
            "Required for graphs with https-transport connector nodes."
        ),
    )
    run_p.add_argument(
        "--audit-plan", default=None, metavar="<json>",
        help=(
            "Path to a JSON file containing an AuditPlan (cross-model audit coverage). "
            "When supplied, the plan is persisted in the run directory so "
            "`bl graph arena` can render the coverage cells and release decision. "
            "Does not affect the run itself — read-side projection only."
        ),
    )
    run_p.add_argument(
        "--max-tokens", default=None, type=int, metavar="<n>",
        help=(
            "Ceiling on the tokens this WHOLE run may spend, across every node. Reaching it "
            "PAUSES the run — it stays resumable — rather than failing it, so no completed "
            "work is thrown away. Resume with a higher ceiling to continue. Overrides "
            "max_tokens from --budget-file."
        ),
    )
    run_p.add_argument(
        "--max-cost-usd", default=None, metavar="<amount>",
        help=(
            "Ceiling on what this whole run may cost, in USD (e.g. 2.50). Needs rates: a "
            "provider reports tokens, not money, so supply a price table via --budget-file or "
            "every cost cap fails closed as unmeasurable. Overrides max_cost_usd from "
            "--budget-file."
        ),
    )
    run_p.add_argument(
        "--budget-file", default=None, metavar="<json>",
        help=(
            "Path to a JSON budget file: {\"max_tokens\": N, \"max_cost_usd\": \"2.50\", "
            "\"prices\": {\"provider/model\": {\"input_microunits_per_mtok\": N, "
            "\"output_microunits_per_mtok\": N}}}. NO default prices ship — a table baked "
            "into the package would be wrong the week a provider repriced, and a stale low "
            "price under-charges, which is the direction that lets a cap permit unauthorised "
            "spend. Explicit flags override this file per dimension."
        ),
    )
    run_p.add_argument("--json", action="store_true", help="Emit JSON output.")
    run_p.set_defaults(func=cmd_graph_run)

    # resume (handler lives in cli_graph_resume.py to keep this file within budget)
    from bounded_loops.graph.cli_graph_resume import add_resume_parser
    add_resume_parser(graph_subs)

    # approve (handler lives in cli_graph_approve.py to keep this file within budget)
    approve_p = graph_subs.add_parser(
        "approve",
        help="Record a human decision for a paused approval-checkpoint node and resume the run.",
        description=(
            "Reads the paused-awaiting-approval status a `bl graph run --execute` reported "
            "and records a durable approve/reject decision for one node, then resumes the "
            "run past it (or fails it closed, for a rejection)."
        ),
        epilog=(
            "Exit codes: 0 success, 2 refused or failed, 3 PAUSED — another\n"
            "approval-checkpoint node (e.g. a later gate in a multi-gate DAG) is still\n"
            "awaiting a decision. Exit code 3 is NOT an error: run this command again\n"
            "for the next node named in its own output.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    approve_p.add_argument("--run", required=True, metavar="<dir>",
                           help="Run directory reported by `bl graph run --execute` (the 'out' path).")
    approve_p.add_argument("--node", required=True, metavar="<node_id>",
                           help="The approval-checkpoint node ID to decide.")
    approve_p.add_argument("--decision", required=True, choices=["approved", "rejected"],
                           help="The human decision to record for this node.")
    approve_p.add_argument(
        "--inputs", default=None, metavar="<json>",
        help=(
            "JSON object mapping node_id -> prompt. Prompts are NOT persisted (C-080); "
            "re-supply one here if a pending connector node needs it to resume."
        ),
    )
    # Same names as `run` and `resume`: approving continues the run, so it needs a ceiling
    # when that run paused on one.
    approve_p.add_argument("--max-tokens", default=None, type=int, metavar="<n>",
                           help="Spend ceiling for the continuation this approval triggers.")
    approve_p.add_argument("--max-cost-usd", default=None, metavar="<amount>",
                           help="Cost ceiling in USD for the continuation this approval triggers.")
    approve_p.add_argument("--budget-file", default=None, metavar="<json>",
                           help="Budget file with standing ceilings and the price table.")
    approve_p.add_argument("--json", action="store_true", help="Emit JSON output.")
    approve_p.set_defaults(func=cmd_graph_approve)

    # arena (handler lives in the arena package to keep this file within budget)
    from bounded_loops.graph.arena.cli_arena import cmd_graph_arena

    arena_p = graph_subs.add_parser(
        "arena",
        help="Render a persisted run into a self-contained, read-only Arena HTML page.",
    )
    arena_p.add_argument("--run", required=True, metavar="<dir>",
                         help="Directory written by `bl graph demo`.")
    arena_p.add_argument("--out", default=None, metavar="<file.html>",
                         help="Output HTML path (default: <run>/arena.html).")
    arena_p.set_defaults(func=cmd_graph_arena)

    # studio (handler lives in the studio package to keep this file within budget)
    from bounded_loops.graph.studio.cli_studio import cmd_graph_studio

    studio_p = graph_subs.add_parser(
        "studio",
        help="Emit the self-contained visual Graph Studio (customizable authoring, no code).",
    )
    studio_p.add_argument("--from", dest="from_manifest", default=None, metavar="<manifest.(yaml|json)>",
                          help="Open an existing graph for editing (validated first).")
    studio_p.add_argument("--out", default=None, metavar="<file.html>",
                          help="Output HTML path (default: ./graph-studio.html).")
    studio_p.set_defaults(func=cmd_graph_studio)

    # console (handler lives in the console package to keep this file within budget)
    from bounded_loops.graph.console.cli_console import cmd_graph_console

    console_p = graph_subs.add_parser(
        "console",
        help="Serve a minimal localhost approval console for one run (click-to-approve).",
        description=(
            "Loopback-only, single-run HTML console: lists paused approval-checkpoint "
            "nodes with Approve/Reject buttons, each driving the SAME "
            "LocalGraphRuntimeFacade.approve() `bl graph approve` uses. LOCAL posture "
            "only — see the printed banner for what a hosted deployment still needs."
        ),
    )
    console_p.add_argument("--run", required=True, metavar="<dir>",
                           help="Run directory reported by `bl graph run --execute` (the 'out' path).")
    console_p.add_argument("--port", type=int, default=0, metavar="<port>",
                           help="TCP port to bind on 127.0.0.1 (default: 0, an OS-assigned ephemeral port).")
    console_p.set_defaults(func=cmd_graph_console)

    # init (handler lives in the init package to keep this file within budget)
    from bounded_loops.graph.init.cli_init import cmd_graph_init

    init_p = graph_subs.add_parser(
        "init",
        help="Interactively configure egress posture and connector mode (writes ~/.bounded-loops/egress.json).",
        description=(
            "Non-technical installer: prompts for egress posture (open/allowlist/broker) and "
            "connector mode (subscription CLI vs BYOK), then writes the config file "
            "resolve_egress_posture() consumes. Defaults to OPEN + subscription-CLI — the "
            "frictionless, recommended path. Providing any of --posture/--connector/--allowlist "
            "runs this non-interactively (fields you omit fall back to their defaults); --yes "
            "additionally skips the confirmation prompt for overwriting an existing config (for "
            "CI). Connector mode and credentials are never written to disk — only egress "
            "posture is."
        ),
    )
    init_p.add_argument(
        "--posture", default=None, choices=["open", "allowlist", "broker"], metavar="<posture>",
        help="Egress posture (default when unset: prompt interactively, or 'open' non-interactively).",
    )
    init_p.add_argument(
        "--allowlist", action="append", default=None, metavar="<host[:port]>",
        help=(
            "Allowlist host (repeatable; each value may also be comma-separated). "
            "Only meaningful with --posture allowlist."
        ),
    )
    init_p.add_argument(
        "--connector", default=None, choices=["local_cli", "byok"], metavar="<mode>",
        help="Connector mode (default: local_cli — your subscription CLI; informational only, no file written).",
    )
    init_p.add_argument(
        "--yes", action="store_true",
        help="Skip confirmation prompts, including overwriting an existing config (for CI / non-interactive use).",
    )
    init_p.add_argument(
        "--config", default=None, metavar="<path>",
        help="Override the egress config file path (mirrors BOUNDED_LOOPS_EGRESS_CONFIG).",
    )
    init_p.set_defaults(func=cmd_graph_init)
