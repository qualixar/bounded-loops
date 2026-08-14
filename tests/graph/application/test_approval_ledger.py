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
from bounded_loops.graph.application.node_contracts import ApprovalOutcome
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
    outcome = resolver.resolve(identity=identity, node=plan.nodes[0], attempt=1, repair_round=0)
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
    outcome = resolver.resolve(identity=identity, node=plan.nodes[0], attempt=1, repair_round=0)
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
    outcome = resolver.resolve(identity=identity, node=plan.nodes[0], attempt=1, repair_round=0)
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


# ── TEST-05: rejection-path error branches (unknown node, malformed attempt) ─
# These mirror the approval-path tests above (test_unknown_node_in_ledger_fails_closed,
# test_foreign_approval_id_fails_closed). The rejection path was added alongside
# approvals but its GraphIntegrityError branches were not mirrored in tests.
# Mutation proof: deleting lines 174 or 181-182 of approval_ledger.py causes
# these tests to NOT raise, making pytest fail on the `pytest.raises` context.

def test_rejection_with_unknown_node_fails_closed(tmp_path: Path) -> None:
    """A rejection record referencing a node not in the plan must raise
    GraphIntegrityError, not silently proceed (fail-closed tamper guard)."""
    from bounded_loops.graph.application.approval_ledger import build_durable_approval_resolver

    plan = _plan()
    identity = _identity(plan)
    aid = _approval_id("ghost-node")
    _write_ledger(tmp_path, {
        "resource_version": 1,
        "commits": [],
        "rejections": [{
            "node_id": "ghost-node",  # not in the plan
            "attempt": 1,
            "approval_id": aid,
            "actor_id": _ORG,
            "decided_at": "2026-08-11T00:00:00Z",
        }],
    })
    with pytest.raises(GraphIntegrityError, match="unknown node"):
        build_durable_approval_resolver(identity=identity, plan=plan, run_dir=tmp_path)


def test_rejection_with_malformed_attempt_fails_closed(tmp_path: Path) -> None:
    """A rejection record with a non-integer attempt field must raise
    GraphIntegrityError. Without this guard a tampered ledger could skip the
    attempt-tracking and bypass the conflict-detection logic."""
    from bounded_loops.graph.application.approval_ledger import build_durable_approval_resolver

    plan = _plan()
    identity = _identity(plan)
    aid = _approval_id("checkpoint")
    _write_ledger(tmp_path, {
        "resource_version": 1,
        "commits": [],
        "rejections": [{
            "node_id": "checkpoint",
            "attempt": "not-an-integer",  # malformed — should be int
            "approval_id": aid,
            "actor_id": _ORG,
            "decided_at": "2026-08-11T00:00:00Z",
        }],
    })
    with pytest.raises(GraphIntegrityError, match="malformed"):
        build_durable_approval_resolver(identity=identity, plan=plan, run_dir=tmp_path)


# ── TEST-11: _load_approvals non-dict root and boolean resource_version ──────
# The existing test covers JSONDecodeError. These two cover the shape-validation
# branches at lines 83 and 90 of approval_ledger.py.
# Mutation proof: removing `if not isinstance(data, dict)` or the bool guard causes
# `_load_approvals` to return the raw data without raising, failing the assertions.

def test_load_approvals_with_array_root_fails_closed(tmp_path: Path) -> None:
    """A ledger whose root is a JSON array (not an object) must raise
    GraphIntegrityError — a valid JSON non-dict should not silently reset state."""
    from bounded_loops.graph.application.approval_ledger import _load_approvals

    path = tmp_path / "approvals.json"
    path.write_text("[]", encoding="utf-8")  # valid JSON, wrong type
    with pytest.raises(GraphIntegrityError, match="malformed"):
        _load_approvals(path)


def test_load_approvals_with_boolean_resource_version_fails_closed(tmp_path: Path) -> None:
    """A ledger with resource_version: true must raise GraphIntegrityError.
    Python's isinstance(True, int) is True, so the bool guard is the only
    protection against a boolean slipping through the integer check."""
    from bounded_loops.graph.application.approval_ledger import _load_approvals

    path = tmp_path / "approvals.json"
    path.write_text(
        '{"resource_version": true, "commits": [], "rejections": []}',
        encoding="utf-8",
    )
    with pytest.raises(GraphIntegrityError, match="resource_version"):
        _load_approvals(path)


# ── re-export surface: graph_runtime_facade must still expose the same names ─

def test_graph_runtime_facade_reexports_load_approvals() -> None:
    """The facade's existing tests import `_load_approvals` from
    `bounded_loops.graph.graph_runtime_facade` — the extraction must keep
    that import path working via re-export, not just move the code out from under it."""
    from bounded_loops.graph.application.approval_ledger import _load_approvals as ledger_fn
    from bounded_loops.graph.graph_runtime_facade import _load_approvals as facade_fn

    assert facade_fn is ledger_fn


def test_graph_runtime_facade_reexports_build_durable_approval_resolver() -> None:
    """execute_graph_run and the facade must call the exact SAME function object —
    proof there is no logic fork between the two call sites."""
    from bounded_loops.graph.application.approval_ledger import (
        build_durable_approval_resolver as ledger_fn,
    )
    from bounded_loops.graph.graph_composition import (
        build_durable_approval_resolver as execute_graph_fn,
    )

    assert ledger_fn is execute_graph_fn


# ── the round survives the durable ledger (P4.5 round-2 audit, Grok 2) ───────────────────


def _commit_record(*, repair_round: object = 1) -> dict:
    aid = _approval_id("checkpoint")
    entry: dict = {
        "approval_id": aid, "new_resource_version": 2, "idempotency_key": aid,
        "node_id": "checkpoint", "actor_id": _ORG, "decided_at": "2026-08-11T00:00:00Z",
    }
    if repair_round is not None:
        entry["repair_round"] = repair_round
    return {"resource_version": 2, "commits": [entry], "rejections": []}


def test_a_durable_grant_carries_the_round_it_was_made_in(tmp_path: Path) -> None:
    """``approvals.json`` records ``repair_round``, and the rehydrated resolver honours it."""
    from bounded_loops.graph.application.approval_ledger import build_durable_approval_resolver

    plan = _plan()
    identity = _identity(plan)
    _write_ledger(tmp_path, _commit_record(repair_round=1))

    resolver = build_durable_approval_resolver(identity=identity, plan=plan, run_dir=tmp_path)

    assert resolver.resolve(
        identity=identity, node=plan.nodes[0], attempt=1, repair_round=1,
    ) is ApprovalOutcome.APPROVED
    assert resolver.resolve(
        identity=identity, node=plan.nodes[0], attempt=1, repair_round=0,
    ) is ApprovalOutcome.PENDING, "a round-1 grant must not authorize round 0 either"


def test_a_record_written_before_the_round_existed_still_resolves_at_round_0(tmp_path: Path) -> None:
    """Backward compatibility is the whole reason round 0 keeps its old coordinates.

    An ``approvals.json`` written by 0.4.0, or by any build before this field existed, has no
    ``repair_round`` key. It must read as round 0 — the round it was actually made in — so an
    existing paused run still resumes on the one decision its operator already gave.
    """
    from bounded_loops.graph.application.approval_ledger import build_durable_approval_resolver

    plan = _plan()
    identity = _identity(plan)
    _write_ledger(tmp_path, _commit_record(repair_round=None))

    resolver = build_durable_approval_resolver(identity=identity, plan=plan, run_dir=tmp_path)

    assert resolver.resolve(
        identity=identity, node=plan.nodes[0], attempt=1, repair_round=0,
    ) is ApprovalOutcome.APPROVED


@pytest.mark.parametrize("junk", ["one", -1, True, 1.5, [1], {"a": 1}])
def test_a_malformed_recorded_round_fails_closed(tmp_path: Path, junk: object) -> None:
    """A decision whose scope cannot be read is not a round-0 decision.

    ``True`` is in the list on purpose: ``isinstance(True, int)`` is True in Python, so a bare
    integer check would accept it and read the round as 1.
    """
    from bounded_loops.graph.application.approval_ledger import build_durable_approval_resolver

    plan = _plan()
    identity = _identity(plan)
    record = _commit_record(repair_round=junk)
    _write_ledger(tmp_path, record)

    with pytest.raises(GraphIntegrityError, match="malformed repair_round"):
        build_durable_approval_resolver(identity=identity, plan=plan, run_dir=tmp_path)
