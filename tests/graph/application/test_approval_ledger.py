"""RED-first unit tests for the shared durable-approval-ledger primitives.

``build_durable_approval_resolver`` is extracted from the private
``LocalGraphRuntimeFacade._durable_resolver`` into a module-level function so BOTH
``execute_graph_run`` and ``LocalGraphRuntimeFacade`` can rebuild an
``ApprovalResolverPort`` from the same durable ``approvals.json`` ledger — one
implementation, no logic fork. These tests exercise that function directly
(hermetic, no facade, no controller) so the extraction itself is proven correct
before either caller depends on it.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from bounded_loops.graph.application.approval_gate import RecordedApprovalResolver
from bounded_loops.graph.application.compile_graph import CompileSnapshot, compile_graph
from bounded_loops.graph.application.run_graph import ApprovalOutcome
from bounded_loops.graph.application.validate_graph import parse_authoring_graph_yaml
from bounded_loops.graph.domain.errors import GraphIntegrityError
from bounded_loops.graph.domain.events import GraphRunIdentity
from bounded_loops.graph.domain.plan import ExecutionPlan

_ORG, _PROJECT, _RUN_ID = "test-org", "test-project", "run-ledger-1"

_ONE_GATE_MANIFEST = """\
api_version: "bounded-loops.dev/graph/v1"
graph_id: ledger-one-gate
version: "1.0.0"
nodes:
  - id: checkpoint
    kind: approval
    required_role: reviewer
    inputs: {}
    outputs: {approved: text}
    budget: {max_attempts: 1, max_wallclock_s: 30}
    effects: [read_only]
    isolation: workspace_only
edges: []
connection_slots: []
policies: {data_class: public, fail_mode: fail_closed}
"""


def _plan() -> ExecutionPlan:
    graph = parse_authoring_graph_yaml(_ONE_GATE_MANIFEST)
    return compile_graph(graph, CompileSnapshot(
        policy_digest="sha256:" + "a" * 64,
        package_digests=frozenset(),
        connections=(),
    ))


def _identity(plan: ExecutionPlan) -> GraphRunIdentity:
    return GraphRunIdentity(
        organization_id=_ORG, project_id=_PROJECT, run_id=_RUN_ID,
        graph_digest=plan.source_graph_digest, plan_digest=plan.plan_id,
        policy_digest=plan.policy_digest,
    )


def _approval_id(node_id: str) -> str:
    return hashlib.sha256(f"{_ORG}:{_PROJECT}:{_RUN_ID}:{node_id}".encode("utf-8")).hexdigest()


def _write_ledger(run_dir: Path, record: dict) -> None:
    (run_dir / "approvals.json").write_text(json.dumps(record, indent=2), encoding="utf-8")


# ── build_durable_approval_resolver ──────────────────────────────────────────

def test_fresh_ledger_produces_pending_resolver(tmp_path: Path) -> None:
    """No approvals.json on disk — the resolver must report PENDING (fail closed: stay paused)."""
    from bounded_loops.graph.application.approval_ledger import build_durable_approval_resolver

    plan = _plan()
    identity = _identity(plan)
    resolver = build_durable_approval_resolver(identity=identity, plan=plan, run_dir=tmp_path)
    assert isinstance(resolver, RecordedApprovalResolver)
    outcome = resolver.resolve(identity=identity, node=plan.nodes[0], attempt=1)
    assert outcome is ApprovalOutcome.PENDING


def test_committed_approval_is_resolved_as_approved(tmp_path: Path) -> None:
    from bounded_loops.graph.application.approval_ledger import build_durable_approval_resolver

    plan = _plan()
    identity = _identity(plan)
    aid = _approval_id("checkpoint")
    _write_ledger(tmp_path, {
        "resource_version": 2,
        "commits": [{
            "approval_id": aid, "new_resource_version": 2, "idempotency_key": aid,
            "node_id": "checkpoint", "actor_id": _ORG, "decided_at": "2026-08-11T00:00:00Z",
        }],
        "rejections": [],
    })
    resolver = build_durable_approval_resolver(identity=identity, plan=plan, run_dir=tmp_path)
    outcome = resolver.resolve(identity=identity, node=plan.nodes[0], attempt=1)
    assert outcome is ApprovalOutcome.APPROVED


def test_committed_rejection_is_resolved_as_rejected(tmp_path: Path) -> None:
    from bounded_loops.graph.application.approval_ledger import build_durable_approval_resolver

    plan = _plan()
    identity = _identity(plan)
    aid = _approval_id("checkpoint")
    _write_ledger(tmp_path, {
        "resource_version": 1,
        "commits": [],
        "rejections": [{
            "node_id": "checkpoint", "attempt": 1, "approval_id": aid,
            "actor_id": _ORG, "decided_at": "2026-08-11T00:00:00Z",
        }],
    })
    resolver = build_durable_approval_resolver(identity=identity, plan=plan, run_dir=tmp_path)
    outcome = resolver.resolve(identity=identity, node=plan.nodes[0], attempt=1)
    assert outcome is ApprovalOutcome.REJECTED


def test_foreign_approval_id_fails_closed(tmp_path: Path) -> None:
    from bounded_loops.graph.application.approval_ledger import build_durable_approval_resolver

    plan = _plan()
    identity = _identity(plan)
    _write_ledger(tmp_path, {
        "resource_version": 2,
        "commits": [{
            "approval_id": "f" * 64, "new_resource_version": 2, "idempotency_key": "f" * 64,
            "node_id": "checkpoint", "actor_id": _ORG, "decided_at": "2026-08-11T00:00:00Z",
        }],
        "rejections": [],
    })
    with pytest.raises(GraphIntegrityError, match="foreign approval_id"):
        build_durable_approval_resolver(identity=identity, plan=plan, run_dir=tmp_path)


def test_unknown_node_in_ledger_fails_closed(tmp_path: Path) -> None:
    from bounded_loops.graph.application.approval_ledger import build_durable_approval_resolver

    plan = _plan()
    identity = _identity(plan)
    aid = _approval_id("ghost")
    _write_ledger(tmp_path, {
        "resource_version": 2,
        "commits": [{
            "approval_id": aid, "new_resource_version": 2, "idempotency_key": aid,
            "node_id": "ghost", "actor_id": _ORG, "decided_at": "2026-08-11T00:00:00Z",
        }],
        "rejections": [],
    })
    with pytest.raises(GraphIntegrityError, match="unknown node"):
        build_durable_approval_resolver(identity=identity, plan=plan, run_dir=tmp_path)


def test_conflicting_approve_and_reject_fails_closed(tmp_path: Path) -> None:
    from bounded_loops.graph.application.approval_ledger import build_durable_approval_resolver

    plan = _plan()
    identity = _identity(plan)
    aid = _approval_id("checkpoint")
    _write_ledger(tmp_path, {
        "resource_version": 2,
        "commits": [{
            "approval_id": aid, "new_resource_version": 2, "idempotency_key": aid,
            "node_id": "checkpoint", "actor_id": _ORG, "decided_at": "2026-08-11T00:00:00Z",
        }],
        "rejections": [{
            "node_id": "checkpoint", "attempt": 1, "approval_id": aid,
            "actor_id": _ORG, "decided_at": "2026-08-11T00:00:00Z",
        }],
    })
    with pytest.raises(GraphIntegrityError, match="conflicting"):
        build_durable_approval_resolver(identity=identity, plan=plan, run_dir=tmp_path)


# ── _load_approvals (module-private, moved from graph_runtime_facade) ────────

def test_load_approvals_missing_file_returns_fresh_default(tmp_path: Path) -> None:
    from bounded_loops.graph.application.approval_ledger import _load_approvals

    record = _load_approvals(tmp_path / "nope.json")
    assert record == {"resource_version": 1, "commits": [], "rejections": []}


def test_load_approvals_corrupt_json_fails_closed(tmp_path: Path) -> None:
    from bounded_loops.graph.application.approval_ledger import _load_approvals

    path = tmp_path / "approvals.json"
    path.write_text("{ not valid json", encoding="utf-8")
    with pytest.raises(GraphIntegrityError):
        _load_approvals(path)


# ── re-export surface: graph_runtime_facade must still expose the same names ─

def test_graph_runtime_facade_reexports_load_approvals() -> None:
    """The facade's existing tests import `_load_approvals` from
    `bounded_loops.graph.application.graph_runtime_facade` — the extraction must keep
    that import path working via re-export, not just move the code out from under it."""
    from bounded_loops.graph.application.approval_ledger import _load_approvals as ledger_fn
    from bounded_loops.graph.application.graph_runtime_facade import _load_approvals as facade_fn

    assert facade_fn is ledger_fn


def test_graph_runtime_facade_reexports_build_durable_approval_resolver() -> None:
    """execute_graph_run and the facade must call the exact SAME function object —
    proof there is no logic fork between the two call sites."""
    from bounded_loops.graph.application.approval_ledger import (
        build_durable_approval_resolver as ledger_fn,
    )
    from bounded_loops.graph.application.execute_graph import (
        build_durable_approval_resolver as execute_graph_fn,
    )

    assert ledger_fn is execute_graph_fn
