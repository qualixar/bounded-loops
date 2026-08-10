"""Graph CLI handlers for `bl graph` — delegates to existing application use cases.

Honesty contract (never violate):
- `run`  : compile a manifest (honest preview); `--execute --out <dir>` REALLY
           runs a graph inside a native OS sandbox (no Docker), proven by an
           independent gate. With NO manifest it runs the built-in demo; with an
           admitted local-CLI manifest (+ --connections/--inputs) it runs that
           graph's agent-CLI nodes for real. BYOK/HTTP and sandboxed tool nodes
           stay refused until their later phases.
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
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from bounded_loops.graph.adapters.persistence.artifact_store import LocalArtifactStore
from bounded_loops.graph.adapters.persistence.artifact_verifier import LocalArtifactVerifier
from bounded_loops.graph.adapters.persistence.event_log import GraphEventLog
from bounded_loops.graph.application.arena_projection import (
    ArenaReadRequest,
    read_arena_projection,
)
from bounded_loops.graph.application.compile_graph import (
    CompileSnapshot,
    compile_graph,
)
from bounded_loops.graph.application.execution_policy import (
    ConfiguredExecutionPolicy,
    ExecutionEnvelope,
    NetworkMode,
)
from bounded_loops.graph.application.run_graph import (
    GateVerdict,
    GraphRunController,
    WorkerResult,
)
from bounded_loops.graph.application.validate_graph import (
    parse_authoring_graph_json,
    parse_authoring_graph_yaml,
)
from bounded_loops.graph.domain.artifacts import ArtifactPolicy
from bounded_loops.graph.domain.connections import ResolvedRoute
from bounded_loops.graph.domain.errors import GraphValidationError
from bounded_loops.graph.domain.events import GraphRunIdentity
from bounded_loops.graph.domain.plan import ExecutionPlan, PlannedNode
# cmd_graph_artifacts lives in a sibling module to keep this file within budget;
# re-exported here so `bl graph artifacts` and existing imports resolve unchanged.
from bounded_loops.graph.cli_graph_artifacts import cmd_graph_artifacts

# ── bundled demonstration manifest ────────────────────────────────────────────

DEMO_MANIFEST_YAML: str = """\
api_version: "bounded-loops.dev/graph/v1"
graph_id: one-node-run
version: "1.0.0"
nodes:
  - id: research
    kind: research_claim
    inputs: {}
    outputs: {claim: text}
    budget: {max_attempts: 1, max_wallclock_s: 1}
    effects: [read_only]
    isolation: workspace_only
    connection_slot: model
edges: []
connection_slots: [{id: model, requires: [text_generation], data_class_max: public}]
policies: {data_class: public, fail_mode: fail_closed}
"""

# JSON-serialisable list; sets are stored as sorted lists so round-trip
# through json.loads works.  CompileSnapshot converts via _candidate_from_raw.
DEMO_CONNECTIONS_LIST: list[dict[str, object]] = [
    {
        "binding_id": "binding-1",
        "slot_id": "model",
        "connector_id": "codex-cli",
        "connector_version": "1.0.0",
        "connection_id": "conn-1",
        "admission_digest": "sha256:" + "b" * 64,
        "route_policy_digest": "sha256:" + "c" * 64,
        "provider_id": "openai",
        "model_target": "codex",
        "region": "in",
        "fallback": False,
        "capabilities": ["text_generation"],
        "data_class_max": "public",
        "allowed_effects": ["read_only"],
        "isolation": "workspace_only",
        "transport": "local_cli",
        "admitted": True,
    }
]

_DEMO_ORG = "demo-org"
_DEMO_PROJECT = "demo-project"
_DEMO_RUN_ID = "demo-run-1"

# ── in-process demonstration collaborators ────────────────────────────────────


@dataclass
class _DemoWorker:
    """Writes one small in-memory artifact per node; returns the real digest."""

    _store: LocalArtifactStore
    _org: str
    _project: str

    def execute(
        self, *, plan: ExecutionPlan, node: PlannedNode, envelope: ExecutionEnvelope
    ) -> WorkerResult:
        content = f"DEMONSTRATION NODE: {node.node_id}".encode("utf-8")
        policy = ArtifactPolicy(
            organization_id=self._org,
            project_id=self._project,
            producer_attempt="1",
            media_type="text/plain",
            sensitivity="public",
            retention_class="standard",
        )
        records = self._store.put_many([(BytesIO(content), policy)])
        digest = records[0].digest
        binding = next(
            (b for b in plan.connection_bindings if b.binding_id == node.binding_id), None
        )
        if binding is None:
            return WorkerResult((digest,))
        route = ResolvedRoute(
            binding.provider_id,
            binding.model_target,
            binding.region,
            binding.fallback,
            binding.route_policy_digest,
        )
        return WorkerResult((digest,), route, binding.transport)


class _DemoGate:
    """Demonstration gate; always passes.  Distinct object from worker."""

    def evaluate(
        self, *, plan: ExecutionPlan, node: PlannedNode, result: WorkerResult
    ) -> GateVerdict:
        return GateVerdict(True, "demonstration gate: always passes")


class _DemoEnforcer:
    """No-op enforcer; the demo makes no isolation or network claims."""

    def enforce(
        self, *, plan: ExecutionPlan, node: PlannedNode, envelope: ExecutionEnvelope
    ) -> None:
        pass


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


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_demo_plan() -> ExecutionPlan:
    graph = parse_authoring_graph_yaml(DEMO_MANIFEST_YAML)
    snapshot = CompileSnapshot(
        policy_digest="sha256:" + "a" * 64,
        package_digests=frozenset(),
        connections=tuple(DEMO_CONNECTIONS_LIST),  # type: ignore[arg-type]
    )
    return compile_graph(graph, snapshot)


def _build_policy(plan: ExecutionPlan) -> ConfiguredExecutionPolicy:
    envelopes: dict[str, ExecutionEnvelope] = {}
    for node in plan.nodes:
        binding = next(
            (b for b in plan.connection_bindings if b.binding_id == node.binding_id), None
        )
        envelopes[node.node_id] = ExecutionEnvelope(
            isolation=node.isolation,
            transport=binding.transport if binding else None,
            allowed_effects=node.required_effects,
            network_mode=NetworkMode.DENY,
            network_destinations=(),
        )
    return ConfiguredExecutionPolicy(envelopes)


def _load_plan_from_run_dir(
    run_dir: Path,
) -> tuple[ExecutionPlan, GraphRunIdentity, dict[str, object]]:
    """Reconstruct plan + identity + raw meta from a persisted run directory."""
    # Symlink guards — reject TOCTOU-capable paths on the run dir and internal files.
    if run_dir.is_symlink():
        raise ValueError(f"run directory '{run_dir}' is a symlink; aborting")
    for _n in ("run-meta.json", "manifest.yaml", "connections.json", "controller-events.jsonl"):
        if (run_dir / _n).is_symlink():
            raise ValueError(f"internal file '{_n}' is a symlink; aborting")

    meta_path = run_dir / "run-meta.json"
    if not meta_path.is_file():
        raise FileNotFoundError(f"run-meta.json not found in {run_dir}")
    try:
        meta: dict[str, object] = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"run-meta.json is not valid JSON: {exc}") from exc
    try:
        policy_digest = str(meta["policy_digest"])
        stored_plan_id = str(meta["plan_id"])
        org_id = str(meta["organization_id"])
        proj_id = str(meta["project_id"])
        run_id_ = str(meta["run_id"])
    except KeyError as exc:
        raise ValueError(f"run-meta.json is missing required key {exc}") from exc

    manifest_text = (run_dir / "manifest.yaml").read_text(encoding="utf-8")
    graph = parse_authoring_graph_yaml(manifest_text)
    raw_connections = json.loads(
        (run_dir / "connections.json").read_text(encoding="utf-8")
    )
    snapshot = CompileSnapshot(
        policy_digest=policy_digest,
        package_digests=frozenset(),
        connections=tuple(raw_connections),  # type: ignore[arg-type]
    )
    plan = compile_graph(graph, snapshot)

    if plan.plan_id != stored_plan_id:
        raise ValueError(
            f"Reconstructed plan_id {plan.plan_id!r} != stored {stored_plan_id!r}"
        )
    identity = GraphRunIdentity(
        organization_id=org_id,
        project_id=proj_id,
        run_id=run_id_,
        graph_digest=plan.source_graph_digest,
        plan_digest=plan.plan_id,
        policy_digest=plan.policy_digest,
    )
    return plan, identity, meta


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
            policy_digest="sha256:" + "a" * 64,
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


def cmd_graph_demo(args: argparse.Namespace) -> int:
    """bl graph demo --out <dir> — DEMONSTRATION; no sandbox or E2 enforcement."""
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    plan = _build_demo_plan()
    identity = GraphRunIdentity(
        organization_id=_DEMO_ORG,
        project_id=_DEMO_PROJECT,
        run_id=_DEMO_RUN_ID,
        graph_digest=plan.source_graph_digest,
        plan_digest=plan.plan_id,
        policy_digest=plan.policy_digest,
    )

    store = LocalArtifactStore(out_dir / "artifacts")
    event_log = GraphEventLog(out_dir / "controller-events.jsonl", identity)
    worker = _DemoWorker(store, _DEMO_ORG, _DEMO_PROJECT)
    gate = _DemoGate()
    verifier = LocalArtifactVerifier(store)
    policy = _build_policy(plan)
    enforcer = _DemoEnforcer()

    controller = GraphRunController(
        plan=plan,
        event_log=event_log,
        worker=worker,
        gate=gate,
        artifact_verifier=verifier,
        execution_policy=policy,
        execution_enforcer=enforcer,
        timestamp=_now_iso,
        actor="graph-controller",
    )
    projection = controller.run()

    # Persist artefacts needed for `status` reconstruction.
    (out_dir / "plan.json").write_bytes(plan.canonical_json)
    (out_dir / "manifest.yaml").write_text(DEMO_MANIFEST_YAML, encoding="utf-8")
    (out_dir / "connections.json").write_text(
        json.dumps(DEMO_CONNECTIONS_LIST, sort_keys=True), encoding="utf-8"
    )
    run_meta = {
        "demonstration": True, "organization_id": _DEMO_ORG,
        "plan_id": plan.plan_id, "policy_digest": plan.policy_digest,
        "project_id": _DEMO_PROJECT, "run_id": _DEMO_RUN_ID,
    }
    (out_dir / "run-meta.json").write_text(
        json.dumps(run_meta, sort_keys=True), encoding="utf-8")

    if projection.state != "SUCCEEDED":
        _err(f"graph demo: run ended with state {projection.state!r}; see event log")
        return 2

    _DEMO_NOTICE = "DEMONSTRATION — nodes are NOT executed in a sandbox; no isolation / network / E2 enforcement."
    banner = "=" * 70 + "\n" + _DEMO_NOTICE + "\n" + "=" * 70

    if getattr(args, "json", False):
        print(json.dumps(
            {
                "demonstration": True,
                "notice": _DEMO_NOTICE,
                "out": str(out_dir),
                "run_id": _DEMO_RUN_ID,
                "run_state": projection.state,
            },
            sort_keys=True,
        ))
    else:
        print(banner)
        print(f"run_state : {projection.state}")
        print(f"run_id    : {_DEMO_RUN_ID}")
        print(f"out       : {out_dir}")
    return 0


_STATUS_NOTICE = "LOCAL/UNVERIFIED — status is read from a local event log; not verified by an Arena server."


def cmd_graph_status(args: argparse.Namespace) -> int:
    """bl graph status --run <dir> — reconstruct plan + read arena projection."""
    run_dir = Path(args.run)
    if not run_dir.is_dir():
        _err(f"graph status: '{run_dir}' is not a directory")
        return 2

    try:
        plan, identity, run_meta = _load_plan_from_run_dir(run_dir)
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


def _execute_manifest(args: argparse.Namespace, manifest: str, out_dir: Path) -> int:
    """Read a user manifest (+ optional --connections/--inputs) and run it for real."""
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
    from bounded_loops.graph.application.execute_graph import execute_graph_run
    return execute_graph_run(
        manifest_text=text,
        manifest_suffix=".json" if suffix == ".json" else ".yaml",
        connections_raw=list(connections_raw),
        node_prompts=node_prompts,
        out_dir=out_dir,
        json_out=getattr(args, "json", False),
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
            from bounded_loops.graph.application.sandbox_demo import run_sandbox_demo
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
            policy_digest="sha256:" + "a" * 64,
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
        description="Subcommands: lint, plan, demo, status, artifacts, run. E2 required for run.",
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
    )
    run_p.add_argument("manifest", nargs="?", default=None, metavar="<manifest.(yaml|json)>",
                       help="Path to the graph manifest file (omit with --execute for the built-in demo).")
    run_p.add_argument("--connections", default=None, metavar="<json>",
                       help="Path to a JSON file containing connection candidates.")
    run_p.add_argument("--inputs", default=None, metavar="<json>",
                       help="JSON object mapping node_id -> prompt (run-time input for local-CLI nodes).")
    run_p.add_argument("--execute", action="store_true",
                       help="Actually execute: the built-in demo (no manifest), or an admitted local-CLI manifest.")
    run_p.add_argument("--out", default=None, metavar="<dir>",
                       help="Output run directory for --execute.")
    run_p.add_argument("--json", action="store_true", help="Emit JSON output.")
    run_p.set_defaults(func=cmd_graph_run)

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
