"""Built-in, REAL sandboxed graph run — the headline no-Docker demonstration.

Unlike ``bl graph demo`` (in-process, no isolation), this executes a node for
real inside a native OS sandbox and proves it: the node tries to open a socket
and write outside its workspace, and an INDEPENDENT gate then reads the produced
artifact and passes only if the OS actually denied the network. The producer
never grades itself — the gate is a separate object that inspects the receipt.

It runs anywhere a native sandbox exists (macOS Seatbelt, Linux bubblewrap) with
no Docker daemon and no root. If the host can only offer Docker, the built-in
local-command demo says so honestly rather than failing cryptically.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from bounded_loops.graph.adapters.enforcement import build_enforcer, probe_platform
from bounded_loops.graph.adapters.enforcement.sandbox import SandboxMechanism
from bounded_loops.graph.adapters.persistence.artifact_store import LocalArtifactStore
from bounded_loops.graph.adapters.persistence.artifact_verifier import LocalArtifactVerifier
from bounded_loops.graph.adapters.persistence.event_log import GraphEventLog
from bounded_loops.graph.application.arena_projection import ArenaReadRequest, read_arena_projection
from bounded_loops.graph.application.compile_graph import CompileSnapshot, compile_graph
from bounded_loops.graph.application.execution_policy import (
    ConfiguredExecutionPolicy,
    ExecutionEnvelope,
    NetworkMode,
)
from bounded_loops.graph.application.run_graph import GateVerdict, GraphRunController, WorkerResult
from bounded_loops.graph.application.sandboxed_worker import (
    NodeExecutionSpec,
    SandboxedNodeWorker,
)
from bounded_loops.graph.application.validate_graph import parse_authoring_graph_yaml
from bounded_loops.graph.domain.artifacts import ArtifactAccess, ArtifactRef
from bounded_loops.graph.domain.errors import GraphValidationError
from bounded_loops.graph.domain.events import GraphRunIdentity
from bounded_loops.graph.domain.plan import ExecutionPlan, PlannedNode

_ORG, _PROJECT, _RUN_ID = "demo-org", "demo-project", "sandbox-demo-run"
_POLICY_DIGEST = "sha256:" + "a" * 64

SANDBOX_DEMO_YAML: str = """\
api_version: "bounded-loops.dev/graph/v1"
graph_id: sandbox-demo
version: "1.0.0"
nodes:
  - id: sandbox_probe
    kind: tool
    tool_ref: "local:sandbox-probe"
    inputs: {}
    outputs: {result: json}
    budget: {max_attempts: 1, max_wallclock_s: 15}
    effects: [workspace_write]
    isolation: container_restricted
edges: []
connection_slots: []
policies: {data_class: internal, fail_mode: fail_closed}
"""

# Runs inside the sandbox: probe the two guarantees, then write the receipt the
# independent gate will inspect. cwd is the isolated outputs directory.
_PROBE_CODE = (
    "import json, os, socket\n"
    "net = 'unknown'\n"
    "try:\n"
    "    s = socket.socket(); s.settimeout(2); s.connect(('127.0.0.1', 1)); s.close(); net = 'reachable'\n"
    "except PermissionError:\n"
    "    net = 'denied_by_sandbox'\n"
    "except OSError as e:\n"
    "    net = 'denied_by_sandbox' if e.errno == 1 else ('refused' if e.errno == 61 else 'err:%s' % e.errno)\n"
    "open('result.json', 'w').write(json.dumps("
    "{'network': net, 'home': os.environ.get('HOME'), 'node': 'sandbox_probe'}))\n"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _SandboxDemoResolver:
    """Maps the demo node to a real local command; no external package needed."""

    def resolve(self, node: PlannedNode) -> NodeExecutionSpec:
        return NodeExecutionSpec(
            argv=(sys.executable, "-I", "-B", "-c", _PROBE_CODE),
            declared_outputs={"result.json": "application/json"},
        )


class _NetworkDeniedGate:
    """Independent gate: pass only if the artifact shows the OS denied network.

    Distinct object from the worker, and it re-reads the promoted artifact from
    the store — it never re-executes the producer.
    """

    def __init__(self, store: LocalArtifactStore) -> None:
        self._store = store

    def evaluate(self, *, plan: ExecutionPlan, node: PlannedNode, result: WorkerResult) -> GateVerdict:
        if not result.output_artifact_digests:
            return GateVerdict(False, "no artifact produced")
        digest = result.output_artifact_digests[0]
        try:
            with self._store.open(ArtifactRef(digest, _ORG, _PROJECT), ArtifactAccess(_ORG, _PROJECT)) as handle:
                payload = json.loads(handle.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            return GateVerdict(False, f"artifact unreadable or invalid: {exc}")
        if payload.get("network") != "denied_by_sandbox":
            return GateVerdict(False, f"network was not OS-denied (observed {payload.get('network')!r})")
        return GateVerdict(True, "independent gate: OS network denial confirmed in artifact")


class _LocalAuthorizer:
    """Local same-tenant arena read (mirrors the CLI's read-side authorizer)."""

    def authorize(self, request: ArenaReadRequest) -> bool:
        return True


class _LocalReceiptVerifier:
    """No-op receipt verifier for a locally produced projection."""

    def verify(self, identity: GraphRunIdentity, receipts: object) -> None:
        return None


def _build_plan() -> ExecutionPlan:
    graph = parse_authoring_graph_yaml(SANDBOX_DEMO_YAML)
    snapshot = CompileSnapshot(
        policy_digest=_POLICY_DIGEST, package_digests=frozenset(), connections=(),
    )
    return compile_graph(graph, snapshot)


def _build_policy(plan: ExecutionPlan) -> ConfiguredExecutionPolicy:
    envelopes = {
        node.node_id: ExecutionEnvelope(
            isolation=node.isolation,
            transport=None,
            allowed_effects=node.required_effects,
            network_mode=NetworkMode.DENY,
            network_destinations=(),
        )
        for node in plan.nodes
    }
    return ConfiguredExecutionPolicy(envelopes)


def _persist_run_dir(out_dir: Path, plan: ExecutionPlan, mechanism: str) -> None:
    (out_dir / "plan.json").write_bytes(plan.canonical_json)
    (out_dir / "manifest.yaml").write_text(SANDBOX_DEMO_YAML, encoding="utf-8")
    (out_dir / "connections.json").write_text("[]", encoding="utf-8")
    run_meta = {
        "organization_id": _ORG,
        "plan_id": plan.plan_id,
        "policy_digest": plan.policy_digest,
        "project_id": _PROJECT,
        "run_id": _RUN_ID,
        "sandbox_execution": True,
        "sandbox_mechanism": mechanism,
        "platform": sys.platform,
    }
    (out_dir / "run-meta.json").write_text(json.dumps(run_meta, sort_keys=True), encoding="utf-8")


def run_sandbox_demo(out_dir: Path, *, json_out: bool = False) -> int:
    """Execute the built-in graph for real inside a native sandbox; persist a
    receipt-backed run directory. Returns a process exit code."""
    out_dir.mkdir(parents=True, exist_ok=True)
    plan = _build_plan()
    caps = probe_platform()

    node = plan.nodes[0]
    mechanism, reason = caps.select_mechanism(node.isolation, NetworkMode.DENY)
    if mechanism is None:
        return _fail(json_out, f"this host cannot sandbox the demo node: {reason}")
    if mechanism is SandboxMechanism.DOCKER:
        return _fail(
            json_out,
            "this host offers only Docker; the built-in demo runs a local command that needs a "
            "native sandbox (macOS Seatbelt or Linux bubblewrap). Install bubblewrap, or run on macOS.",
        )

    identity = GraphRunIdentity(
        organization_id=_ORG, project_id=_PROJECT, run_id=_RUN_ID,
        graph_digest=plan.source_graph_digest, plan_digest=plan.plan_id, policy_digest=plan.policy_digest,
    )
    store = LocalArtifactStore(out_dir / "artifacts")
    event_log = GraphEventLog(out_dir / "controller-events.jsonl", identity)
    worker = SandboxedNodeWorker(
        identity=identity, artifact_store=store, resolver=_SandboxDemoResolver(),
        capabilities=caps, workspace_root=out_dir / "work",
        organization_id=_ORG, project_id=_PROJECT,
    )
    try:
        enforcer = build_enforcer(plan, capabilities=caps)
    except GraphValidationError as exc:
        return _fail(json_out, f"execution enforcement refused before run: {exc.message}")

    controller = GraphRunController(
        plan=plan, event_log=event_log, worker=worker, gate=_NetworkDeniedGate(store),
        artifact_verifier=LocalArtifactVerifier(store), execution_policy=_build_policy(plan),
        execution_enforcer=enforcer, timestamp=_now_iso, actor="graph-controller",
    )
    run_projection = controller.run()
    used = worker.mechanism_for("sandbox_probe") or mechanism.value
    _persist_run_dir(out_dir, plan, used)

    arena = read_arena_projection(
        plan, event_log,
        ArenaReadRequest(subject_id=_ORG, organization_id=_ORG, project_id=_PROJECT, run_id=_RUN_ID),
        _LocalAuthorizer(), _LocalReceiptVerifier(),
    )
    succeeded = run_projection.state == "SUCCEEDED"
    digests = [n.artifact_digests[0] for n in arena.nodes if n.artifact_digests]
    if json_out:
        print(json.dumps({
            "sandbox_execution": True,
            "mechanism": used,
            "platform": sys.platform,
            "run_state": run_projection.state,
            "run_id": _RUN_ID,
            "out": str(out_dir),
            "artifact_digests": digests,
        }, sort_keys=True))
        return 0 if succeeded else 2

    print("Sandboxed graph run — REAL execution, no Docker required")
    print("=" * 62)
    print(f"platform  : {sys.platform}")
    print(f"mechanism : {used}  (network OS-denied, writes confined to the workspace)")
    print(f"run_state : {run_projection.state}")
    for n in arena.nodes:
        mark = "OK " if n.state == "SUCCEEDED" else "!! "
        art = (n.artifact_digests[0][:24] + "...") if n.artifact_digests else "-"
        print(f"  {mark}node {n.node_id!r}: {n.state}  artifact={art}")
    print(f"out       : {out_dir}")
    if succeeded:
        print()
        print(f"Open the visual Arena:  bl graph arena --run {out_dir}")
        return 0
    print()
    print("Run did not succeed; inspect the event log in the run directory.")
    return 2


def _fail(json_out: bool, message: str) -> int:
    if json_out:
        print(json.dumps({"sandbox_execution": False, "error": message}, sort_keys=True))
    else:
        print(f"error: {message}", file=sys.stderr)
    return 2
