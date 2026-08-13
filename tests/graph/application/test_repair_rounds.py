"""Repair rounds end to end: the bounded outer loop, its receipts, and the termination bound.

A repair points BACKWARDS — a downstream failure re-runs an upstream node. That breaks the premise
the whole replay verifier rests on (terminal states absorb, predecessors are monotonic), so the
boundary that permits it is checked harder than anything else in the engine.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest


from bounded_loops.graph.adapters.persistence.event_log import GraphEventLog
from bounded_loops.graph.application.arena_projection import latest_node_states
from bounded_loops.graph.application.compile_graph import CompileSnapshot, compile_graph
from bounded_loops.graph.application.execution_policy import (
    ConfiguredExecutionPolicy,
    ExecutionEnvelope,
    NetworkMode,
)
from bounded_loops.graph.application.node_contracts import GateVerdict, WorkerResult
from bounded_loops.graph.application.repair_rounds import (
    REPAIR_ROUND_EVENT,
    assert_boundary_is_legal,
    descendants,
    next_repair_round,
    repair_budget,
    rounds_spent,
    total_execution_bound,
)
from bounded_loops.graph.application.run_graph import GraphRunController
from bounded_loops.graph.application.validate_graph import validate_authoring_graph
from bounded_loops.graph.domain.errors import GraphIntegrityError
from bounded_loops.graph.domain.events import GraphRunIdentity, UnsignedGraphEvent

_DIGEST = "sha256:" + "d" * 64


def _node(node_id: str, attempts: int = 1, **extra: object) -> dict[str, object]:
    return {
        "id": node_id, "kind": "research_claim", "inputs": {}, "outputs": {"out": "text"},
        "budget": {"max_attempts": attempts, "max_wallclock_s": 1},
        "effects": ["read_only"], "isolation": "workspace_only", **extra,
    }


def _plan(*, budget: int = 2, attempts: int = 1, repair: bool = True):
    """``fetch -> shape -> verify``; ``verify`` repairs ``fetch`` (a grandparent).

    ``repair=False`` drops the declaration, because a graph declaring repair with a budget of 0
    is refused at validation — the budget is what makes termination provable.
    """
    third = _node("verify", attempts, inputs={"feed": "text"})
    if repair:
        third["on_failure"] = {"mode": "repair", "target": "fetch"}
    graph = validate_authoring_graph({
        "api_version": "bounded-loops.dev/graph/v1", "graph_id": "repair-run",
        "version": "1.0.0",
        "nodes": [
            _node("fetch", attempts),
            _node("shape", attempts, inputs={"feed": "text"}),
            third,
        ],
        "edges": [
            {"from_node": "fetch", "from_port": "out", "to_node": "shape", "to_port": "feed",
             "when": None},
            {"from_node": "shape", "from_port": "out", "to_node": "verify", "to_port": "feed",
             "when": None},
        ],
        "connection_slots": [],
        "policies": {
            "data_class": "public", "fail_mode": "continue_declared", "repair_budget": budget,
        },
    })
    return compile_graph(graph, CompileSnapshot(
        policy_digest="sha256:" + "a" * 64, package_digests=frozenset(), connections=(),
    ))


def _identity(plan) -> GraphRunIdentity:
    return GraphRunIdentity(
        organization_id="org-1", project_id="project-1", run_id="run-1",
        graph_digest=plan.source_graph_digest, plan_digest=plan.plan_id,
        policy_digest=plan.policy_digest,
    )


@dataclass
class _Worker:
    calls: list[str]

    def execute(self, *, plan, node, envelope, attempt=1) -> WorkerResult:
        self.calls.append(node.node_id)
        # No route or transport: these nodes are UNBOUND (no connection slots), and the controller
        # validates an observed route against the binding before verifying artifacts.
        return WorkerResult((_DIGEST,), None, None)


@dataclass
class _GateRejecting:
    reject: str
    calls: list[str]

    def evaluate(self, *, plan, node, result) -> GateVerdict:
        self.calls.append(node.node_id)
        return GateVerdict(node.node_id != self.reject, "selective fixture gate")


@dataclass
class _Artifacts:
    def verify(self, *, identity, digests) -> None:
        if not set(digests) <= {_DIGEST}:
            raise GraphIntegrityError("unknown artifact")


class _Enforcer:
    def enforce(self, *, plan, node, envelope) -> None:
        return None


def _policy(plan) -> ConfiguredExecutionPolicy:
    return ConfiguredExecutionPolicy({
        node.node_id: ExecutionEnvelope(
            node.isolation,
            next(
                (b.transport for b in plan.connection_bindings if b.binding_id == node.binding_id),
                None,
            ),
            node.required_effects,
            NetworkMode.DENY,
            (),
        )
        for node in plan.nodes
    })


def _controller(plan, log, worker, gate=None):
    return GraphRunController(
        plan=plan, event_log=log, worker=worker,
        gate=gate or _GateRejecting("__none__", []),
        artifact_verifier=_Artifacts(), execution_policy=_policy(plan),
        execution_enforcer=_Enforcer(), timestamp=lambda: "2026-08-14T00:00:00Z",
        continue_on_failure=True,
    )


# ── the pure pieces ───────────────────────────────────────────────────────────────────────


def test_the_suffix_is_the_target_and_its_descendants_only():
    """Condition 1 of the bound. A repair must not silently redo unrelated work."""
    plan = _plan()
    assert descendants(plan, "fetch") == {"fetch", "shape", "verify"}
    assert descendants(plan, "shape") == {"shape", "verify"}
    assert descendants(plan, "verify") == {"verify"}


def test_the_termination_bound_is_one_plus_R_times_the_per_round_total():
    """``(1 + R) * Σ_v (b_v + 1)`` — the quantity arXiv:2604.11378 Thm 6.2 gets wrong under repair.

    Its bound is the sum of the per-node budgets and its proof needs terminal states to absorb; a
    repair breaks that, and the true bound carries the ``(1 + R)`` factor.
    """
    assert repair_budget(_plan(budget=2)) == 2
    # 3 nodes x 1 attempt = 3 per round; (1 + 2) rounds
    assert total_execution_bound(_plan(budget=2, attempts=1)) == 9
    assert total_execution_bound(_plan(budget=0, attempts=1, repair=False)) == 3
    assert total_execution_bound(_plan(budget=1, attempts=2)) == 12


def test_no_round_is_opened_when_nothing_declaring_repair_has_failed():
    plan = _plan()
    assert next_repair_round(plan, {"fetch": "SUCCEEDED", "shape": "SUCCEEDED",
                                    "verify": "SUCCEEDED"}, ()) is None


def test_a_round_is_opened_for_the_failed_repairing_node():
    plan = _plan()
    assert next_repair_round(
        plan, {"fetch": "SUCCEEDED", "shape": "SUCCEEDED", "verify": "FAILED"}, (),
    ) == ("verify", "fetch", 1)


def test_the_GLOBAL_budget_stops_further_rounds():
    """Counted from the receipts, so a resumed run cannot start its budget over."""
    plan = _plan(budget=1)
    states = {"fetch": "SUCCEEDED", "shape": "SUCCEEDED", "verify": "FAILED"}
    spent = (_boundary_event(1),)
    assert rounds_spent(spent) == 1
    assert next_repair_round(plan, states, spent) is None


def _boundary_event(round_index: int):
    from bounded_loops.graph.domain.events import StoredGraphEvent
    return StoredGraphEvent(
        identity=_identity(_plan()), sequence=round_index,
        event=UnsignedGraphEvent(
            event_id=f"e{round_index}", idempotency_key=f"k{round_index}",
            event_type=REPAIR_ROUND_EVENT, timestamp="t", actor="c",
            payload={"round": round_index, "target_node": "fetch",
                     "trigger_node": "verify", "reason": "r"},
        ),
        previous_hash="0" * 64, event_hash="1" * 64,
    )


# ── end to end through the controller ─────────────────────────────────────────────────────


def test_a_repair_round_really_re_runs_the_target_and_its_descendants(tmp_path):
    """The whole point of P4.25b: ``verify`` fails, ``fetch`` runs AGAIN, and so does ``shape``."""
    plan = _plan(budget=1)
    worker = _Worker([])
    log = GraphEventLog(tmp_path / "events.jsonl", _identity(plan))

    projection = _controller(plan, log, worker, gate=_GateRejecting("verify", [])).run()

    # round 0: fetch, shape, verify(fails) -> round 1: fetch, shape, verify(fails again) -> seal
    assert worker.calls == ["fetch", "shape", "verify", "fetch", "shape", "verify"]
    assert projection.state == "FAILED"
    events = [e.event.event_type for e in log.replay()]
    assert events.count(REPAIR_ROUND_EVENT) == 1
    assert events.count("node.repaired") == 3  # fetch, shape, verify all reset


def test_the_repair_boundary_records_who_failed_and_what_is_being_repaired(tmp_path):
    plan = _plan(budget=1)
    log = GraphEventLog(tmp_path / "events.jsonl", _identity(plan))
    _controller(plan, log, _Worker([]), gate=_GateRejecting("verify", [])).run()

    boundary = [
        e.event.payload for e in log.replay() if e.event.event_type == REPAIR_ROUND_EVENT
    ][0]
    assert boundary["round"] == 1
    assert boundary["trigger_node"] == "verify"
    assert boundary["target_node"] == "fetch"
    assert "verify" in str(boundary["reason"])

    repaired = [e.event.payload for e in log.replay() if e.event.event_type == "node.repaired"]
    # from_state proves a terminal state was deliberately abandoned, not corrupted
    assert {str(p["node_id"]): str(p["from_state"]) for p in repaired} == {
        "fetch": "SUCCEEDED", "shape": "SUCCEEDED", "verify": "FAILED",
    }


def test_a_repaired_run_stays_inside_the_bound_and_terminates(tmp_path):
    """Termination is the claim; the bound is the number. Both are checked from the log."""
    plan = _plan(budget=2)
    worker = _Worker([])
    log = GraphEventLog(tmp_path / "events.jsonl", _identity(plan))

    projection = _controller(plan, log, worker, gate=_GateRejecting("verify", [])).run()

    assert projection.state == "FAILED"          # it stopped
    assert len(worker.calls) <= total_execution_bound(plan)
    assert len(worker.calls) == 9                # (1 + 2) rounds x 3 nodes
    assert rounds_spent(log.replay()) == 2       # never exceeds the global budget


def test_a_repaired_run_replays_through_the_receipt_verifier(tmp_path):
    """The verifier must accept a boundary it can prove legal — and rebuild the reset itself."""
    plan = _plan(budget=1)
    log = GraphEventLog(tmp_path / "events.jsonl", _identity(plan))
    _controller(plan, log, _Worker([]), gate=_GateRejecting("verify", [])).run()

    states = latest_node_states(plan, log.replay())

    assert states["verify"]["state"] == "FAILED"
    assert states["fetch"]["state"] == "SUCCEEDED"  # re-ran in round 1 and succeeded again


def test_a_run_with_no_repair_declared_opens_no_rounds(tmp_path):
    """Backward compatibility: repair costs nothing when it is not asked for."""
    plan = _plan(budget=0, repair=False)
    log = GraphEventLog(tmp_path / "events.jsonl", _identity(plan))
    _controller(plan, log, _Worker([])).run()
    assert rounds_spent(log.replay()) == 0



# ── forged boundaries: the one place state may move backward is checked hardest ────────────


@dataclass
class _GateRejectingTimes:
    """Rejects ``node`` its first ``times`` evaluations, then passes.

    Lets a repair actually SUCCEED, so a run can finish with budget left over — which is what the
    forged-boundary tests need: a forged round must be able to carry the correct next number and
    still be inside the budget, or the budget check masks the check under test.
    """

    reject: str
    times: int
    seen: list[str]

    def evaluate(self, *, plan, node, result) -> GateVerdict:
        self.seen.append(node.node_id)
        if node.node_id != self.reject:
            return GateVerdict(True, "selective fixture gate")
        return GateVerdict(self.seen.count(self.reject) > self.times, "selective fixture gate")


def _repaired_run(tmp_path, plan):
    """One honest repair round, then success — so budget remains for a forged boundary."""
    log = GraphEventLog(tmp_path / "events.jsonl", _identity(plan))
    projection = _controller(
        plan, log, _Worker([]), gate=_GateRejectingTimes("verify", 1, []),
    ).run()
    assert projection.state == "SUCCEEDED"
    assert rounds_spent(log.replay()) == 1
    return log


def _log_with_failed(tmp_path, plan, failed: str, cause: str = "gate_rejected"):
    """An honest prefix, hand-built through the production writer, ending with ``failed`` FAILED and
    ZERO repair rounds spent.

    Built by hand rather than by running the controller because the controller would immediately open
    a repair round of its own — leaving no budget, so the budget check would mask the specific check
    each attack below is aiming at.
    """
    log = GraphEventLog(tmp_path / f"events-{failed}-{cause}.jsonl", _identity(plan))
    head = log.append("0" * 64, UnsignedGraphEvent(
        event_id="c", idempotency_key="c", event_type="run.created",
        timestamp="t", actor="controller", payload={"state": "PENDING"},
    )).event_hash
    head = log.append(head, UnsignedGraphEvent(
        event_id="s", idempotency_key="s", event_type="run.started",
        timestamp="t", actor="controller", payload={"state": "RUNNING"},
    )).event_hash
    for node_id in ("fetch", "shape", "verify"):
        for event, state in (
            ("node.ready", "READY"), ("node.starting", "STARTING"), ("node.running", "RUNNING"),
        ):
            head = log.append(head, UnsignedGraphEvent(
                event_id=f"{node_id}-{state}", idempotency_key=f"{node_id}-{state}",
                event_type=event, timestamp="t", actor="controller",
                payload={"node_id": node_id, "state": state, "attempt": 1},
            )).event_hash
        if node_id == failed:
            head = log.append(head, UnsignedGraphEvent(
                event_id=f"{node_id}-bad", idempotency_key=f"{node_id}-bad",
                event_type="node.failed", timestamp="t", actor="controller",
                payload={
                    "node_id": node_id, "state": "FAILED", "attempt": 1,
                    "reason": f"failed: {cause}", "cause": cause,
                    **(
                        {"verdict": {"passed": False, "reason": "no"}}
                        if cause == "gate_rejected" else {}
                    ),
                },
            )).event_hash
            break
        head = log.append(head, UnsignedGraphEvent(
            event_id=f"{node_id}-gating", idempotency_key=f"{node_id}-gating",
            event_type="node.gating", timestamp="t", actor="controller",
            payload={"node_id": node_id, "state": "GATING", "attempt": 1},
        )).event_hash
        head = log.append(head, UnsignedGraphEvent(
            event_id=f"{node_id}-ok", idempotency_key=f"{node_id}-ok",
            event_type="node.succeeded", timestamp="t", actor="controller",
            payload={
                "node_id": node_id, "state": "SUCCEEDED", "attempt": 1,
                "artifact_digests": [_DIGEST],
            },
        )).event_hash
    return log


def _forge(log, event_type: str, key: str, payload: dict) -> None:
    """Append a correctly hash-chained event the controller would never have written."""
    receipts = log.replay()
    head = receipts[-1].event_hash if receipts else "0" * 64
    log.append(head, UnsignedGraphEvent(
        event_id=key, idempotency_key=key, event_type=event_type,
        timestamp="2026-08-14T00:00:00Z", actor="forged", payload=payload,
    ))


def test_a_boundary_whose_trigger_did_not_FAIL_is_refused(tmp_path):
    """The core attack: a boundary resets terminal states, which are the evidence a reader trusts.
    Claiming a node triggered a repair when it succeeded would erase real outcomes."""
    plan = _plan(budget=2)
    log = GraphEventLog(tmp_path / "events.jsonl", _identity(plan))
    _controller(plan, log, _Worker([])).run()  # everything succeeds
    _forge(log, REPAIR_ROUND_EVENT, "forged-round", {
        "round": 1, "target_node": "fetch", "trigger_node": "verify", "reason": "forged",
    })

    with pytest.raises(GraphIntegrityError, match="not FAILED"):
        latest_node_states(plan, log.replay())


def test_a_boundary_triggered_by_a_node_that_declares_no_repair_is_refused(tmp_path):
    plan = _plan(budget=2)
    log = _log_with_failed(tmp_path, plan, "shape")
    _forge(log, REPAIR_ROUND_EVENT, "forged-round-2", {
        "round": 1, "target_node": "fetch", "trigger_node": "shape", "reason": "forged",
    })

    with pytest.raises(GraphIntegrityError, match="declares no repair policy"):
        latest_node_states(plan, log.replay())


def test_a_boundary_repairing_a_node_the_trigger_did_not_declare_is_refused(tmp_path):
    """Suffix locality: repairing a wider or unrelated target would silently redo other work."""
    plan = _plan(budget=2)
    log = _log_with_failed(tmp_path, plan, "verify")
    _forge(log, REPAIR_ROUND_EVENT, "forged-round-3", {
        "round": 1, "target_node": "shape", "trigger_node": "verify", "reason": "forged",
    })

    with pytest.raises(GraphIntegrityError, match="declares 'fetch'"):
        latest_node_states(plan, log.replay())


def test_a_boundary_beyond_the_GLOBAL_budget_is_refused():
    """Condition 3 of the bound, enforced by the predicate the replay path calls.

    Tested here rather than through a forged log because reaching a second boundary on a replay
    requires forging an entire honest round after the first reset — the lifecycle correctly refuses a
    PENDING node jumping straight to FAILED. The rule itself is what matters, and this is where it
    lives: without it a log could repair without limit and no reader would notice.
    """
    plan = _plan(budget=1)
    # Round 1 is legal on this graph...
    assert_boundary_is_legal(
        plan, round_index=1, trigger_node="verify", target_node="fetch", trigger_state="FAILED",
        trigger_cause="gate_rejected",
    )
    # ...round 2 is one too many, even though everything else about it is honest.
    with pytest.raises(GraphIntegrityError, match="exceeds the graph's global repair budget"):
        assert_boundary_is_legal(
            plan, round_index=2, trigger_node="verify", target_node="fetch",
            trigger_state="FAILED", trigger_cause="gate_rejected",
        )


def test_a_zero_round_boundary_is_refused():
    """Rounds are numbered from 1; a 0th round would make the count meaningless."""
    with pytest.raises(GraphIntegrityError, match="exceeds the graph's global repair budget"):
        assert_boundary_is_legal(
            _plan(budget=2), round_index=0, trigger_node="verify", target_node="fetch",
            trigger_state="FAILED", trigger_cause="gate_rejected",
        )


def test_boundaries_must_be_numbered_consecutively(tmp_path):
    """A gap would let a log hide a round, and the round count is what the budget is measured on."""
    plan = _plan(budget=3)
    log = _repaired_run(tmp_path, plan)
    # One honest round is spent, so the next legal number is 2. Declaring 3 hides a round.
    _forge(log, REPAIR_ROUND_EVENT, "forged-skip", {
        "round": 3, "target_node": "fetch", "trigger_node": "verify", "reason": "forged",
    })

    with pytest.raises(GraphIntegrityError, match="numbered consecutively"):
        latest_node_states(plan, log.replay())


def test_a_boundary_naming_a_node_outside_the_plan_is_refused(tmp_path):
    plan = _plan(budget=2)
    log = _repaired_run(tmp_path, plan)
    _forge(log, REPAIR_ROUND_EVENT, "forged-outside", {
        "round": 2, "target_node": "ghost", "trigger_node": "verify", "reason": "forged",
    })

    with pytest.raises(GraphIntegrityError, match="outside the immutable plan"):
        latest_node_states(plan, log.replay())


@pytest.mark.parametrize(
    "halting",
    ["gate_broken", "policy_denied", "environment_denied", "approval_rejected",
     "spend_exhausted", "no_worker", "worker_contract", "budget_unmeasurable"],
)
def test_a_HALT_class_failure_may_not_be_repaired(halting):
    """Muse finding 3. Checking only the STATE let a hand-chained log repair past a broken gate or a
    denied policy — rewinding a run the live controller would have sealed.

    The live path could never do it (a halting cause never reaches a repair), so this was a
    replay-only hole: exactly the kind a forged-but-hash-valid log exploits.
    """
    with pytest.raises(GraphIntegrityError, match="stops the run whatever the fail mode"):
        assert_boundary_is_legal(
            _plan(budget=2), round_index=1, trigger_node="verify", target_node="fetch",
            trigger_state="FAILED", trigger_cause=halting,
        )


@pytest.mark.parametrize(
    "continuable",
    ["gate_rejected", "worker_fault", "artifact_unverified", "budget_spent", "redrive_exhausted"],
)
def test_every_continue_eligible_failure_may_be_repaired(continuable):
    """The other direction: a repair must remain possible after the node's own bounded-loop outcome,
    or the feature is unreachable."""
    assert_boundary_is_legal(
        _plan(budget=2), round_index=1, trigger_node="verify", target_node="fetch",
        trigger_state="FAILED", trigger_cause=continuable,
    )


@pytest.mark.parametrize("missing", [None, "", 7, "not_a_cause"])
def test_a_boundary_with_no_usable_failure_cause_is_refused(missing):
    """Refused rather than waved through: pre-0.5 logs carry no repair boundaries at all, so an
    absent or unknown cause on one can only be a forgery or a corruption."""
    with pytest.raises(GraphIntegrityError):
        assert_boundary_is_legal(
            _plan(budget=2), round_index=1, trigger_node="verify", target_node="fetch",
            trigger_state="FAILED", trigger_cause=missing,
        )


def test_a_forged_boundary_after_a_BROKEN_GATE_is_refused_on_replay(tmp_path):
    """The same rule through the log path, which is where the attack actually lands."""
    plan = _plan(budget=2)
    log = _log_with_failed(tmp_path, plan, "verify", cause="gate_broken")
    _forge(log, REPAIR_ROUND_EVENT, "forged-halt-repair", {
        "round": 1, "target_node": "fetch", "trigger_node": "verify", "reason": "forged",
    })

    with pytest.raises(GraphIntegrityError, match="stops the run whatever the fail mode"):
        latest_node_states(plan, log.replay())
