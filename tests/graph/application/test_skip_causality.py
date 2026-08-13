"""SKIPPED must not be a back door out of PENDING.

Before edge guards, READY was the ONLY exit from PENDING, and the replay verifier relied on that:
it checked cross-node causality once, at PENDING->READY, and argued that was sufficient and sound.
Adding SKIPPED as a second exit invalidates that argument unless the new exit is checked too.

These are the attacks a fully re-hash-chained forged log can mount once it can write ``node.skipped``.
Every per-node lifecycle below is individually legal; only the causality check rejects them.
"""

from __future__ import annotations

import pytest

from bounded_loops.graph.adapters.persistence.event_log import GraphEventLog
from bounded_loops.graph.application.arena_projection import latest_node_states
from bounded_loops.graph.domain.authoring import Effect, IsolationLevel
from bounded_loops.graph.domain.errors import GraphIntegrityError
from bounded_loops.graph.domain.events import GraphRunIdentity, UnsignedGraphEvent
from bounded_loops.graph.domain.plan import ExecutionPlan, PlannedEdge, PlannedNode


def _plan(guard: str | None) -> ExecutionPlan:
    def node(node_id: str) -> PlannedNode:
        return PlannedNode(
            node_id=node_id, kind="research_claim", package_digest=None, binding_id=None,
            required_effects=frozenset({Effect.READ_ONLY}),
            isolation=IsolationLevel.WORKSPACE_ONLY, hard_deadline_ms=1000,
            budgets={"max_attempts": 1}, approval_policy={},
        )

    return ExecutionPlan(
        api_version="bounded-loops.dev/plan/v1", plan_id="sha256:" + "b" * 64,
        source_graph_digest="sha256:" + "a" * 64, policy_digest="sha256:" + "c" * 64,
        compiler_version="bounded-loops.graph-compiler/v1",
        nodes=(node("a"), node("b")),
        edges=(PlannedEdge("a", "out", "b", "feed", guard),),
        levels=(("a",), ("b",)), package_digests=(), connection_bindings=(),
        canonical_json=b'{"plan":"skip-causality-fixture"}',
    )


def _identity(plan: ExecutionPlan) -> GraphRunIdentity:
    return GraphRunIdentity(
        organization_id="org-1", project_id="project-1", run_id="run-1",
        graph_digest=plan.source_graph_digest, plan_digest=plan.plan_id,
        policy_digest=plan.policy_digest,
    )


def _append(store: GraphEventLog, head: str, event_type: str, key: str, payload: dict) -> str:
    return store.append(
        head,
        UnsignedGraphEvent(
            event_id=f"event-{key}", idempotency_key=key, event_type=event_type,
            timestamp="2026-08-13T00:00:00Z", actor="controller", payload=payload,
        ),
    ).event_hash


def _opened(store: GraphEventLog) -> str:
    head = _append(store, "0" * 64, "run.created", "created", {"state": "PENDING"})
    return _append(store, head, "run.started", "started", {"state": "RUNNING"})


def _succeed(store: GraphEventLog, head: str, node_id: str) -> str:
    for event, state in (
        ("node.ready", "READY"), ("node.starting", "STARTING"),
        ("node.running", "RUNNING"), ("node.gating", "GATING"),
    ):
        head = _append(store, head, event, f"{node_id}-{state}", {
            "node_id": node_id, "state": state, "attempt": 1,
        })
    return _append(store, head, "node.succeeded", f"{node_id}-ok", {
        "node_id": node_id, "state": "SUCCEEDED", "attempt": 1, "artifact_digests": [],
    })


def _fail(store: GraphEventLog, head: str, node_id: str) -> str:
    for event, state in (
        ("node.ready", "READY"), ("node.starting", "STARTING"), ("node.running", "RUNNING"),
    ):
        head = _append(store, head, event, f"{node_id}-{state}", {
            "node_id": node_id, "state": state, "attempt": 1,
        })
    return _append(store, head, "node.failed", f"{node_id}-bad", {
        "node_id": node_id, "state": "FAILED", "attempt": 1, "reason": "worker crashed",
        "cause": "worker_fault",
    })


def test_an_honest_skip_of_an_untaken_branch_replays(tmp_path):
    """Guard ``failed`` with a SUCCEEDED source: the branch was genuinely not taken."""
    plan = _plan("failed")
    store = GraphEventLog(tmp_path / "events.jsonl", _identity(plan))
    head = _succeed(store, _opened(store), "a")
    _append(store, head, "node.skipped", "b-skip", {
        "node_id": "b", "state": "SKIPPED", "attempt": 0, "reason": "branch not taken",
    })

    states = latest_node_states(plan, store.replay())

    assert states["b"]["state"] == "SKIPPED"
    assert states["b"]["attempt"] == 0


def test_a_forged_skip_cannot_dodge_an_unsatisfied_UNGUARDED_dependency(tmp_path):
    """The primary attack. ``b`` has an unguarded edge from a FAILED ``a``, so admission is BLOCK.

    Marking ``b`` SKIPPED would let the run report SUCCEEDED-or-skipped and walk past a real
    dependency failure. Nothing but the causality check stops this: the lifecycle is legal.
    """
    plan = _plan(None)
    store = GraphEventLog(tmp_path / "events.jsonl", _identity(plan))
    head = _fail(store, _opened(store), "a")
    _append(store, head, "node.skipped", "b-skip", {
        "node_id": "b", "state": "SKIPPED", "attempt": 0, "reason": "forged",
    })

    with pytest.raises(GraphIntegrityError, match="did not authorise"):
        latest_node_states(plan, store.replay())


def test_a_forged_skip_cannot_retire_a_branch_that_WAS_taken(tmp_path):
    """Guard ``succeeded`` with a SUCCEEDED source: admission is ADMIT, so SKIPPED is a lie.

    Without this, a log could silently drop any node it did not want to account for.
    """
    plan = _plan("succeeded")
    store = GraphEventLog(tmp_path / "events.jsonl", _identity(plan))
    head = _succeed(store, _opened(store), "a")
    _append(store, head, "node.skipped", "b-skip", {
        "node_id": "b", "state": "SKIPPED", "attempt": 0, "reason": "forged",
    })

    with pytest.raises(GraphIntegrityError, match="did not authorise"):
        latest_node_states(plan, store.replay())


def test_a_forged_dispatch_cannot_run_a_branch_that_was_NOT_taken(tmp_path):
    """The mirror attack: guard ``failed`` with a SUCCEEDED source means SKIP, so READY is a lie.

    This direction was already rejected before SKIPPED existed, and must stay rejected — the two
    exits must not become interchangeable.
    """
    plan = _plan("failed")
    store = GraphEventLog(tmp_path / "events.jsonl", _identity(plan))
    head = _succeed(store, _opened(store), "a")
    _append(store, head, "node.ready", "b-ready", {
        "node_id": "b", "state": "READY", "attempt": 1,
    })

    with pytest.raises(GraphIntegrityError, match="did not authorise"):
        latest_node_states(plan, store.replay())


def test_a_skip_may_not_advance_the_attempt_count(tmp_path):
    """A skipped node ran nothing. Attempt 1 would assert work that never happened, and inflate
    any per-attempt count derived from the log."""
    plan = _plan("failed")
    store = GraphEventLog(tmp_path / "events.jsonl", _identity(plan))
    head = _succeed(store, _opened(store), "a")
    _append(store, head, "node.skipped", "b-skip", {
        "node_id": "b", "state": "SKIPPED", "attempt": 1, "reason": "branch not taken",
    })

    with pytest.raises(GraphIntegrityError, match="attempt sequence is invalid"):
        latest_node_states(plan, store.replay())


def test_a_skip_written_without_a_reason_is_refused_on_append(tmp_path):
    plan = _plan("failed")
    store = GraphEventLog(tmp_path / "events.jsonl", _identity(plan))
    head = _succeed(store, _opened(store), "a")

    with pytest.raises(GraphIntegrityError, match="invalid shape"):
        _append(store, head, "node.skipped", "b-skip", {
            "node_id": "b", "state": "SKIPPED", "attempt": 0,
        })


def test_a_skipped_node_is_terminal_and_cannot_be_revived(tmp_path):
    plan = _plan("failed")
    store = GraphEventLog(tmp_path / "events.jsonl", _identity(plan))
    head = _succeed(store, _opened(store), "a")
    head = _append(store, head, "node.skipped", "b-skip", {
        "node_id": "b", "state": "SKIPPED", "attempt": 0, "reason": "branch not taken",
    })
    _append(store, head, "node.ready", "b-ready", {
        "node_id": "b", "state": "READY", "attempt": 1,
    })

    with pytest.raises(GraphIntegrityError, match="lifecycle is invalid"):
        latest_node_states(plan, store.replay())
