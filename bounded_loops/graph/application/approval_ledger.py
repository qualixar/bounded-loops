"""Durable approval-ledger primitives shared across the graph engine (C-080 follow-up).

``approvals.json`` is the durable, file-backed ledger of human approval/rejection
decisions for one run directory. This module holds the READ-SIDE primitives — loading
the ledger and rebuilding an ``ApprovalResolverPort`` from it — so BOTH
``execute_graph_run`` (a fresh run that may pause at an approval node) and
``LocalGraphRuntimeFacade`` (resume/approve over an existing run) share exactly ONE
implementation. There is no logic fork between the two call sites.

Import-graph placement is deliberate: ``execute_graph.py`` builds the controller for a
fresh run, and ``graph_runtime_facade.py`` imports FROM ``execute_graph.py``
(``build_execution_controller``). If the durable-resolver logic lived in
``graph_runtime_facade.py`` instead, ``execute_graph.py`` importing it back would be a
circular import. This module sits BELOW both of them (it depends only on
``approval_gate``, ``approvals``, and domain modules), so both can import from it
without ever importing each other.

The WRITE side (``_FileApprovalCommandPort``, ``_atomic_write``) stays in
``graph_runtime_facade.py`` — only ``LocalGraphRuntimeFacade.approve()`` ever writes a
decision; ``execute_graph_run`` never does.

LOCAL TRUST POSTURE — honest boundary, NOT a tamper-proof claim: ``approvals.json`` is a
plain file, not hash-chained like ``controller-events.jsonl``. The deterministic
``approval_id`` is a run-scoped HANDLE derived from public identity — it corroborates
that a record belongs to this run+node, but it is NOT a credential. So anyone who can
WRITE the run directory is trusted as the operator (the local posture: the CLI/MCP
session is the authentication boundary and the run dir is single-tenant local FS). A
HOSTED / multi-tenant deployment MUST make decisions tamper-EVIDENT — sign each record
and re-verify it via an injected signature verifier, or chain each decision into the
hash-chained receipt log — and MUST NOT treat run-dir writability as approval authority.

Two SPECIFIC gaps in this local posture, stated plainly rather than left implicit (both
surfaced by dual-audit review — do not claim tamper-evidence that does not exist):

* The REJECT path (``_FileApprovalCommandPort.commit_rejection`` in
  ``graph_runtime_facade.py``) does NOT run any signature verification — only the
  APPROVE path calls ``approval_signature_verifier.verify(...)`` (via the
  ``approvals.approve`` use case). A local actor with run-dir write access can
  therefore durably reject a node with no attestation at all, whereas approving the
  same node requires a non-empty signature. This asymmetry is ACCEPTABLE for the local
  trust boundary (the FS write is already the authority) but is a residual TODO for a
  HOSTED deployment: verify rejections too — either by routing them through a signed
  decision object and an injected verifier, or by chaining them into the hash-chained
  receipt log — before treating a rejection as tamper-evident.
* ``LocalGraphRuntimeFacade.approve()`` does NOT require the target node's CURRENT
  projected state to already be ``AWAITING_APPROVAL`` before it durably commits a
  decision — a caller can record an approval for a node the run has not yet reached
  (e.g. pre-clearing gate 2 of a two-gate DAG while gate 1 is still pending). Locally
  this is harmless — the decision is simply honored WHEN the run reaches that node —
  but a regulated/hosted HITL deployment that needs "the human saw the hold, not just
  the request" must additionally bind the decision to the node's current hold evidence
  (e.g. its ``AWAITING_APPROVAL`` receipt digest) and reject a decision that predates it.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from bounded_loops.graph.application.approval_gate import RecordedApprovalResolver
from bounded_loops.graph.application.approvals import ApprovalCommit
from bounded_loops.graph.domain.approvals import ApprovalRequest
from bounded_loops.graph.domain.errors import GraphIntegrityError
from bounded_loops.graph.domain.events import GraphRunIdentity
from bounded_loops.graph.domain.plan import ExecutionPlan, PlannedNode


def _load_approvals(path: Path) -> dict:
    """Load the durable approval ledger, or a fresh empty one if it does not exist yet.

    A MISSING ledger is a legitimate fresh start; a CORRUPT/torn ledger must FAIL CLOSED
    — never silently reset to version 1 (that would wipe the idempotency/version guard
    and let a decision be re-committed).
    """
    if not path.is_file():
        return {"resource_version": 1, "commits": [], "rejections": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise GraphIntegrityError(f"approval ledger is unreadable or corrupt: {path}") from exc
    if not isinstance(data, dict):
        raise GraphIntegrityError("approval ledger is malformed")
    # Shape-validate every field the version guard + rehydration rely on, so a malformed
    # ledger fails closed HERE (GraphIntegrityError) rather than leaking a raw
    # KeyError/AttributeError downstream (e.g. a non-list ``commits`` or a string
    # ``resource_version``).
    version = data.get("resource_version", 1)
    if isinstance(version, bool) or not isinstance(version, int):
        raise GraphIntegrityError("approval ledger resource_version must be an integer")
    for key in ("commits", "rejections"):
        entries = data.get(key, [])
        if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
            raise GraphIntegrityError(f"approval ledger {key} must be a list of objects")
    return data


def _approval_id(identity: GraphRunIdentity, node_id: str) -> str:
    """Deterministic approval identity for a run+node — the SAME derivation on the
    commit path and the durable-rehydration path, so a re-honored approval matches
    exactly the one that was persisted (and a durable record whose id does not match
    this derivation is rejected as foreign)."""
    return hashlib.sha256(
        f"{identity.organization_id}:{identity.project_id}:{identity.run_id}:{node_id}".encode("utf-8")
    ).hexdigest()


def _rehydrated_request(identity: GraphRunIdentity, node: PlannedNode) -> ApprovalRequest:
    # record_committed_approval reads only approval_id + tenant + node_id + attempt; the
    # remaining fields are reconstructed deterministically and are never re-validated on
    # the rehydration path.
    return ApprovalRequest(
        approval_id=_approval_id(identity, node.node_id),
        organization_id=identity.organization_id,
        project_id=identity.project_id,
        graph_digest=identity.graph_digest,
        plan_digest=identity.plan_digest,
        node_id=node.node_id,
        attempt=1,
        evidence_digest="sha256:" + "0" * 64,  # unused by the resolver guard
        requested_effects=frozenset(node.required_effects),
        required_role=str(node.approval_policy.get("required_role") or "reviewer"),
        nonce=hashlib.sha256(f"{identity.run_id}:{node.node_id}:nonce".encode("utf-8")).hexdigest(),
        expires_at="",
    )


def build_durable_approval_resolver(
    *, identity: GraphRunIdentity, plan: ExecutionPlan, run_dir: Path,
) -> RecordedApprovalResolver:
    """Rebuild an ``ApprovalResolverPort`` from the DURABLE ``approvals.json`` ledger.

    Crash-recovery / cross-call sharing: an approval or rejection that was durably
    committed BEFORE this call (by an earlier ``execute_graph_run`` attempt at the same
    ``run_dir``, or by a prior ``LocalGraphRuntimeFacade.approve()``) is honored here, so
    the run never re-pauses a gate a human already decided. A MISSING ledger (the common
    case for a brand-new run) produces an empty resolver — every approval node PAUSES
    until a decision is recorded.

    Every entry is validated fail-closed: an unknown node, a malformed entry, a foreign
    ``approval_id`` (not the deterministic id for THIS run+node), or a node carrying BOTH
    a durable approval and a rejection raises ``GraphIntegrityError``
    (``_load_approvals`` fails closed first on a torn/mis-shaped file).
    """
    resolver = RecordedApprovalResolver()
    record = _load_approvals(run_dir / "approvals.json")
    nodes_by_id = {n.node_id: n for n in plan.nodes}
    approved: set[tuple[str, int]] = set()
    rejected: set[tuple[str, int]] = set()

    for stored in record.get("commits", []):
        node_id = str(stored.get("node_id", ""))
        node = nodes_by_id.get(node_id)
        if node is None:
            raise GraphIntegrityError(f"durable approval references unknown node {node_id!r}")
        if stored.get("approval_id") != _approval_id(identity, node_id):
            raise GraphIntegrityError(f"durable approval for node {node_id!r} has a foreign approval_id")
        try:
            commit = ApprovalCommit(
                approval_id=str(stored["approval_id"]),
                new_resource_version=int(stored["new_resource_version"]),
                idempotency_key=str(stored["idempotency_key"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise GraphIntegrityError(f"durable approval record for node {node_id!r} is malformed") from exc
        resolver.record_committed_approval(
            identity=identity, request=_rehydrated_request(identity, node), commit=commit,
        )
        approved.add((node_id, 1))

    for stored in record.get("rejections", []):
        node_id = str(stored.get("node_id", ""))
        if node_id not in nodes_by_id:
            raise GraphIntegrityError(f"durable rejection references unknown node {node_id!r}")
        # Rejections carry the SAME deterministic-id guard as approvals — never a weaker
        # forgery/DoS surface.
        if stored.get("approval_id") != _approval_id(identity, node_id):
            raise GraphIntegrityError(f"durable rejection for node {node_id!r} has a foreign approval_id")
        try:
            attempt = int(stored.get("attempt", 1))
        except (TypeError, ValueError) as exc:
            raise GraphIntegrityError(f"durable rejection record for node {node_id!r} is malformed") from exc
        resolver.record_rejection(identity=identity, node_id=node_id, attempt=attempt)
        rejected.add((node_id, attempt))

    # Node-level conflict: a node must never carry BOTH decisions, regardless of attempt —
    # a tamper that recorded them under different attempts must still fail closed.
    conflict_nodes = {node_id for node_id, _ in approved} & {node_id for node_id, _ in rejected}
    if conflict_nodes:
        raise GraphIntegrityError(
            f"conflicting durable approve+reject decisions for nodes {sorted(conflict_nodes)!r}"
        )
    return resolver
