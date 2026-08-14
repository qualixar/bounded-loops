"""Wiring for non-connector node kinds: package roots, workers, gates, and kind-based dispatch.

Extracted from ``graph_composition`` when that module crossed the 800-line cap. It holds the pieces
that make loop, join, and publish nodes runnable: where packages are found, the workers and gates
for each kind, and ``build_kind_dispatchers`` — the single assembly function
``build_execution_controller`` calls instead of constructing the dispatchers inline.

``_is_nontransport_kind`` identifies node kinds that skip the connector-transport preflight check;
``_NONTRANSPORT_KINDS`` is its backing set for fast membership tests.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from bounded_loops.application.manifest import LoopManifest, load as load_manifest
from bounded_loops.graph.adapters.enforcement.capabilities import PlatformCapabilities
from bounded_loops.graph.adapters.workers.acceptance_gate import StructuralAcceptanceGate
from bounded_loops.graph.adapters.workers.join_worker import JoinNodeWorker, JoinReceiptGate
from bounded_loops.graph.adapters.workers.loop_packages import (
    DEFAULT_OUTCOME_FILENAME,
    LoopNodeResolver,
    LoopPackageRegistry,
)
from bounded_loops.graph.adapters.workers.loop_receipt_gate import LoopReceiptGate
from bounded_loops.graph.adapters.workers.publish_worker import (
    LocalPublicationLedger,
    PublishNodeWorker,
    PublishReceiptGate,
)
from bounded_loops.graph.adapters.workers.sandboxed_worker import SandboxedNodeWorker
from bounded_loops.graph.application.execution_policy import ExecutionEnvelope
from bounded_loops.graph.application.graph_ports import ArtifactStorePort, EventLogPort
from bounded_loops.graph.application.node_contracts import GateVerdict, WorkerResult
from bounded_loops.graph.application.workspace_promotion import WorkspaceInput
from bounded_loops.graph.domain.artifacts import ArtifactRef
from bounded_loops.graph.domain.authoring import NodeKind
from bounded_loops.graph.domain.errors import GraphIntegrityError
from bounded_loops.graph.domain.events import GraphRunIdentity
from bounded_loops.graph.domain.plan import ExecutionPlan, PlannedEdge, PlannedNode

#: Node kinds that have their own workers and skip the connector-transport preflight check.
_NONTRANSPORT_KINDS = frozenset({
    NodeKind.APPROVAL.value,
    NodeKind.LOOP.value,
    NodeKind.JOIN.value,
    NodeKind.PUBLISH.value,
})


def _is_nontransport_kind(kind: str) -> bool:
    """True for node kinds that run without a connector transport."""
    return kind in _NONTRANSPORT_KINDS


def _make_upstream_digests_reader(
    event_log: EventLogPort,
) -> Callable[[str], tuple[str, ...]]:
    """Return a callable that retrieves artifact digests for the most-recent SUCCEEDED receipt.

    The callable is passed a node_id and returns the artifact digests tuple from the last
    ``node.succeeded`` event for that node, or an empty tuple if the node has not succeeded yet.
    Walking in REVERSE means we always get the last-attempt result, which is the one the
    promotion machinery sealed.
    """
    def _read(node_id: str) -> tuple[str, ...]:
        for stored in reversed(event_log.replay()):
            payload = stored.event.payload
            if (
                stored.event.event_type == "node.succeeded"
                and payload.get("node_id") == node_id
            ):
                artifacts = payload.get("artifact_digests", ())
                if isinstance(artifacts, (list, tuple)):
                    return tuple(str(d) for d in artifacts if isinstance(d, str))
        return ()
    return _read


def _build_extra_declared_outputs(
    manifest: LoopManifest,
) -> tuple[tuple[str, str], ...]:
    """(path, media_type) pairs for declared output ports.

    Paths use ``outputs/<port_name>`` so they sort AFTER ``loop-outcome.json`` alphabetically.
    That invariant keeps ``LoopReceiptGate.evaluate`` pointing at ``digests[0]`` without changes.
    """
    return tuple(
        (f"outputs/{port.name}", port.media_type)
        for port in manifest.outputs.values()
    )


def _build_loop_input_artifacts(
    *,
    manifest: LoopManifest,
    node: PlannedNode,
    plan: ExecutionPlan,
    registry: LoopPackageRegistry,
    upstream_digests_fn: Callable[[str], tuple[str, ...]],
    organization_id: str,
    project_id: str,
) -> tuple[WorkspaceInput, ...]:
    """Resolve each declared input port to the upstream artifact that satisfies it.

    For each input port:
    1. Find the incoming edge to this node for that port.
    2. Load the upstream loop's manifest to know its declared output paths.
    3. Compute the sorted artifact index (mirrors what the sandboxed worker promotes).
    4. Fetch that digest from the upstream node's event-log SUCCEEDED receipt.
    5. Build a WorkspaceInput so the sandboxed worker materialises it in BL_GRAPH_INPUTS.
    """
    edge_index: dict[str, PlannedEdge] = {
        edge.to_port: edge
        for edge in plan.edges
        if edge.to_node == node.node_id
    }
    result: list[WorkspaceInput] = []
    for port in manifest.inputs.values():
        edge = edge_index.get(port.name)
        if edge is None:
            if port.required:
                raise GraphIntegrityError(
                    f"loop node {node.node_id!r}: required input port {port.name!r} "
                    "has no incoming edge in the execution plan"
                )
            continue
        upstream_node = next(
            (n for n in plan.nodes if n.node_id == edge.from_node), None
        )
        if upstream_node is None or upstream_node.package_digest is None:
            raise GraphIntegrityError(
                f"loop node {node.node_id!r}: upstream node {edge.from_node!r} "
                "is not a resolved loop node (missing package_digest)"
            )
        upstream_package = registry.resolve(upstream_node.package_digest)
        upstream_manifest = load_manifest(upstream_package)
        # Sorted declared paths mirror the promotion order — alphabetical by relative path.
        upstream_paths = sorted(
            [DEFAULT_OUTCOME_FILENAME]
            + [f"outputs/{p}" for p in upstream_manifest.outputs]
        )
        artifact_path = f"outputs/{edge.from_port}"
        try:
            artifact_index = upstream_paths.index(artifact_path)
        except ValueError:
            raise GraphIntegrityError(
                f"loop node {node.node_id!r}: upstream node {edge.from_node!r} "
                f"does not declare output port {edge.from_port!r}"
            ) from None
        upstream_digests = upstream_digests_fn(edge.from_node)
        if artifact_index >= len(upstream_digests):
            raise GraphIntegrityError(
                f"loop node {node.node_id!r}: upstream node {edge.from_node!r} succeeded "
                f"but artifact at index {artifact_index} (port {edge.from_port!r}) "
                "is missing from its receipt — node may not have completed yet"
            )
        result.append(WorkspaceInput(
            target_path=port.name,
            artifact=ArtifactRef(
                digest=upstream_digests[artifact_index],
                organization_id=organization_id,
                project_id=project_id,
            ),
        ))
    return tuple(result)


def _default_loop_roots() -> tuple[Path, ...]:
    """Where loop packages are searched when a caller names no roots.

    Both entries are ordinary directories of candidate packages; resolution is still BY DIGEST, so
    adding a root can only make a package FINDABLE, never change which package a given digest means.
    That is why a cwd-relative root is safe here: it cannot redirect an admitted digest to different
    code, only fail to find it.
    """
    shipped = Path(__file__).resolve().parents[2] / "loops"
    roots: list[Path] = []
    for candidate in (shipped, Path.cwd() / "loops"):
        # Deduplicated by RESOLVED path: run from inside the repo and these two are the same
        # directory, which would otherwise digest all 68 packages twice on every assembly.
        if candidate.is_dir() and not any(root.samefile(candidate) for root in roots):
            roots.append(candidate)
    return tuple(roots)


class _UnsupportedNodeWorker:
    """Fail closed for a node this phase cannot run (the preflight surfaces it first)."""

    def execute(
        self, *, plan: ExecutionPlan, node: PlannedNode, envelope: ExecutionEnvelope,
        attempt: int,
    ) -> WorkerResult:
        raise GraphIntegrityError(
            f"node {node.node_id!r} (kind {node.kind!r}) is not runnable via "
            "`bl graph run --execute` (admitted local-CLI or https connector nodes, and "
            "`kind: loop` nodes whose loop_package resolves on this host)"
        )


@dataclass(frozen=True)
class _LoopNodeWorker:
    """Run a ``kind: loop`` node's package by delegating to the real sandboxed worker.

    The resolver is rebuilt per attempt rather than held, because ``NodeExecutionResolver.resolve``
    takes only the node — so the attempt has to be closed over, and a resolver carrying mutable
    attempt state would be a race as soon as two nodes run at once. ``dataclasses.replace`` keeps
    that substitution immutable.

    ``upstream_digests_fn`` is optional so fixture graphs (no declared ports, no event log) are
    unchanged: when absent, input port wiring is skipped and the loop runs without overlaid inputs.
    """

    sandboxed: SandboxedNodeWorker
    registry: LoopPackageRegistry
    run_id: str
    upstream_digests_fn: Callable[[str], tuple[str, ...]] | None = None

    def execute(
        self, *, plan: ExecutionPlan, node: PlannedNode, envelope: ExecutionEnvelope,
        attempt: int,
    ) -> WorkerResult:
        input_artifacts: tuple[WorkspaceInput, ...] = ()
        extra_declared_outputs: tuple[tuple[str, str], ...] = ()
        if node.package_digest is not None:
            try:
                manifest = load_manifest(self.registry.resolve(node.package_digest))
            except GraphIntegrityError:
                manifest = None
            if manifest is not None:
                if manifest.inputs and self.upstream_digests_fn is not None:
                    input_artifacts = _build_loop_input_artifacts(
                        manifest=manifest, node=node, plan=plan,
                        registry=self.registry,
                        upstream_digests_fn=self.upstream_digests_fn,
                        organization_id=self.sandboxed.organization_id,
                        project_id=self.sandboxed.project_id,
                    )
                if manifest.outputs:
                    extra_declared_outputs = _build_extra_declared_outputs(manifest)
        resolver = LoopNodeResolver(
            registry=self.registry, run_id=self.run_id, attempt=attempt,
            # Round 0 is SOUND here rather than an assumption, because a loop node that declares
            # `on_failure: repair` is refused at validation: the round cannot reach a worker through
            # `NodeWorkerPort.execute`, and stamping round-1 work as round 0 would put a false round
            # in a signed receipt. Lifting that refusal means carrying the round on the port, exactly
            # as `attempt` already is.
            repair_round=0,
            input_artifacts=input_artifacts,
            extra_declared_outputs=extra_declared_outputs,
        )
        return replace(self.sandboxed, resolver=resolver).execute(
            plan=plan, node=node, envelope=envelope, attempt=attempt,
        )


@dataclass(frozen=True)
class _KindDispatchWorker:
    """Route each node to the worker for its kind; fail closed for kinds with no worker."""

    loop_worker: _LoopNodeWorker
    join_worker: JoinNodeWorker
    publish_worker: PublishNodeWorker
    fallback: _UnsupportedNodeWorker

    def execute(
        self, *, plan: ExecutionPlan, node: PlannedNode, envelope: ExecutionEnvelope,
        attempt: int,
    ) -> WorkerResult:
        if node.kind == NodeKind.LOOP.value:
            return self.loop_worker.execute(plan=plan, node=node, envelope=envelope, attempt=attempt)
        if node.kind == NodeKind.JOIN.value:
            return self.join_worker.execute(plan=plan, node=node, envelope=envelope, attempt=attempt)
        if node.kind == NodeKind.PUBLISH.value:
            return self.publish_worker.execute(
                plan=plan, node=node, envelope=envelope, attempt=attempt,
            )
        return self.fallback.execute(plan=plan, node=node, envelope=envelope, attempt=attempt)


@dataclass(frozen=True)
class _KindDispatchGate:
    """Route each node to the gate for its kind.

    A loop node's gate must verify the RECEIPT, not re-read a reply as text: the loop already
    contains its own independent gate, so re-running that check here would make one object both
    producer and judge. ``StructuralAcceptanceGate`` would also pass any non-empty UTF-8 artifact,
    which a loop outcome recording ``HALT`` trivially is — so using it for loop nodes would accept
    a loop that never converged. Join and publish nodes carry causal receipts that similarly require
    their own evidence-verifying gates rather than the generic structural check.
    """

    loop_gate: LoopReceiptGate
    join_gate: JoinReceiptGate
    publish_gate: PublishReceiptGate
    fallback: StructuralAcceptanceGate

    def evaluate(
        self, *, plan: ExecutionPlan, node: PlannedNode, result: WorkerResult,
    ) -> GateVerdict:
        if node.kind == NodeKind.LOOP.value:
            return self.loop_gate.evaluate(plan=plan, node=node, result=result)
        if node.kind == NodeKind.JOIN.value:
            return self.join_gate.evaluate(plan=plan, node=node, result=result)
        if node.kind == NodeKind.PUBLISH.value:
            return self.publish_gate.evaluate(plan=plan, node=node, result=result)
        return self.fallback.evaluate(plan=plan, node=node, result=result)


def build_kind_dispatchers(
    *,
    store: ArtifactStorePort,
    event_log: EventLogPort,
    identity: GraphRunIdentity,
    out_dir: Path,
    caps: PlatformCapabilities,
    loop_package_roots: tuple[Path, ...] | None,
    organization_id: str,
    project_id: str,
    run_id: str,
) -> tuple[_KindDispatchWorker, _KindDispatchGate]:
    """Assemble the kind-dispatch worker+gate pair for all locally runnable node kinds.

    Called once per controller assembly from ``build_execution_controller``.  Moving
    this construction out of ``graph_composition`` keeps that module under the 800-line
    cap and groups all kind-specific wiring in one place.
    """
    loop_registry = LoopPackageRegistry(roots=loop_package_roots or _default_loop_roots())
    loop_worker = _LoopNodeWorker(
        sandboxed=SandboxedNodeWorker(
            identity=identity,
            artifact_store=store,
            # Replaced per attempt by _LoopNodeWorker; this placeholder is never resolved through.
            resolver=LoopNodeResolver(registry=loop_registry, run_id=run_id),
            capabilities=caps,
            workspace_root=out_dir / "work",
            organization_id=organization_id,
            project_id=project_id,
        ),
        registry=loop_registry,
        run_id=run_id,
        upstream_digests_fn=_make_upstream_digests_reader(event_log),
    )
    join_worker = JoinNodeWorker(
        store=store, organization_id=organization_id, project_id=project_id,
    )
    ledger = LocalPublicationLedger(out_dir / "published-effects.json")
    publish_worker = PublishNodeWorker(
        store=store, ledger=ledger, run_id=run_id,
        organization_id=organization_id, project_id=project_id,
    )
    return (
        _KindDispatchWorker(
            loop_worker=loop_worker,
            join_worker=join_worker,
            publish_worker=publish_worker,
            fallback=_UnsupportedNodeWorker(),
        ),
        _KindDispatchGate(
            loop_gate=LoopReceiptGate(
                store, organization_id=organization_id, project_id=project_id,
            ),
            join_gate=JoinReceiptGate(
                store, organization_id=organization_id, project_id=project_id,
            ),
            publish_gate=PublishReceiptGate(
                store, run_id=run_id, organization_id=organization_id, project_id=project_id,
            ),
            fallback=StructuralAcceptanceGate(
                store, organization_id=organization_id, project_id=project_id,
            ),
        ),
    )


def admitted_loop_package_digests(roots: tuple[Path, ...] | None = None) -> frozenset[str]:
    """Every loop-package digest resolvable on this host, in the ``sha256:`` form a plan declares.

    ``compile_graph._validate_packages`` refuses a ``kind: loop`` node whose ``loop_package`` is not
    in ``CompileSnapshot.package_digests``. Every caller passed ``frozenset()``, so that check
    refused EVERY loop node unconditionally — which is why a graph could name a package and still
    only ever run its connector binding.

    "Admitted" here means "this host can produce these exact bytes". That is the honest reading and
    it keeps the refusal meaningful: a digest for a package that is not present still fails at
    compile, with a pointer to the node that named it, instead of failing later inside a sandbox.
    Nothing is weakened by admitting the whole local catalogue, because resolution is BY DIGEST and
    the entry point re-hashes the bytes before it runs them.
    """
    registry = LoopPackageRegistry(roots=roots if roots is not None else _default_loop_roots())
    return frozenset(f"sha256:{digest}" for digest in registry.index())
