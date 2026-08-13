"""Wiring for ``kind: loop`` nodes: package roots, the worker, and kind-based dispatch.

Extracted from ``graph_composition`` when that module crossed the 800-line cap. It holds the pieces
that make a loop node runnable and nothing else: where packages are found, the worker that delegates
to the real sandboxed one, and the two dispatchers that route a node to the worker and gate for its
kind.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from bounded_loops.graph.adapters.workers.acceptance_gate import StructuralAcceptanceGate
from bounded_loops.graph.adapters.workers.loop_packages import (
    LoopNodeResolver,
    LoopPackageRegistry,
)
from bounded_loops.graph.adapters.workers.loop_receipt_gate import LoopReceiptGate
from bounded_loops.graph.adapters.workers.sandboxed_worker import SandboxedNodeWorker
from bounded_loops.graph.application.execution_policy import ExecutionEnvelope
from bounded_loops.graph.application.node_contracts import GateVerdict, WorkerResult
from bounded_loops.graph.domain.authoring import NodeKind
from bounded_loops.graph.domain.errors import GraphIntegrityError
from bounded_loops.graph.domain.plan import ExecutionPlan, PlannedNode


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
    """

    sandboxed: SandboxedNodeWorker
    registry: LoopPackageRegistry
    run_id: str

    def execute(
        self, *, plan: ExecutionPlan, node: PlannedNode, envelope: ExecutionEnvelope,
        attempt: int,
    ) -> WorkerResult:
        resolver = LoopNodeResolver(
            registry=self.registry, run_id=self.run_id, attempt=attempt,
            # Round 0 is SOUND here rather than an assumption, because a loop node that declares
            # `on_failure: repair` is refused at validation: the round cannot reach a worker through
            # `NodeWorkerPort.execute`, and stamping round-1 work as round 0 would put a false round
            # in a signed receipt. Lifting that refusal means carrying the round on the port, exactly
            # as `attempt` already is.
            repair_round=0,
        )
        return replace(self.sandboxed, resolver=resolver).execute(
            plan=plan, node=node, envelope=envelope, attempt=attempt,
        )


@dataclass(frozen=True)
class _KindDispatchWorker:
    """Route each node to the worker for its kind; fail closed for kinds with no worker."""

    loop_worker: _LoopNodeWorker
    fallback: _UnsupportedNodeWorker

    def execute(
        self, *, plan: ExecutionPlan, node: PlannedNode, envelope: ExecutionEnvelope,
        attempt: int,
    ) -> WorkerResult:
        worker = self.loop_worker if node.kind == NodeKind.LOOP.value else self.fallback
        return worker.execute(plan=plan, node=node, envelope=envelope, attempt=attempt)


@dataclass(frozen=True)
class _KindDispatchGate:
    """Route each node to the gate for its kind.

    A loop node's gate must verify the RECEIPT, not re-read a reply as text: the loop already
    contains its own independent gate, so re-running that check here would make one object both
    producer and judge. ``StructuralAcceptanceGate`` would also pass any non-empty UTF-8 artifact,
    which a loop outcome recording ``HALT`` trivially is — so using it for loop nodes would accept
    a loop that never converged.
    """

    loop_gate: LoopReceiptGate
    fallback: StructuralAcceptanceGate

    def evaluate(
        self, *, plan: ExecutionPlan, node: PlannedNode, result: WorkerResult,
    ) -> GateVerdict:
        gate = self.loop_gate if node.kind == NodeKind.LOOP.value else self.fallback
        return gate.evaluate(plan=plan, node=node, result=result)


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
