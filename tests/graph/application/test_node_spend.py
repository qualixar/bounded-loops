"""Spend is metered per attempt, accumulated from the receipts, and enforced before work.

Before this, ``max_tokens`` and ``max_cost_microunits`` were declared in the authoring schema
and read by nothing: validation REFUSED them outright with "no component meters it". These
tests pin the behaviour that replaces that refusal, and specifically the three properties a
spend bound is worthless without:

* a REJECTED attempt's spend is recorded — otherwise retry walks through any cap while every
  recorded total stays small,
* the total is re-derived from the durable log — otherwise `kill -9` in a loop resets it, and
* an unmeasurable attempt is refused, not metered as free — otherwise the cap never trips and
  the operator cannot tell that from being protected.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bounded_loops.graph.adapters.persistence.event_log import GraphEventLog
from bounded_loops.graph.application.arena_projection import latest_node_states
from bounded_loops.graph.application.node_contracts import WorkerResult
from bounded_loops.graph.application.node_spend import (
    NodeSpend,
    RunBudget,
    consumed_spend_from,
    run_spend,
    spend_refusal,
)
from bounded_loops.graph.application.execution_policy import (
    ConfiguredExecutionPolicy,
    ExecutionEnvelope,
    NetworkMode,
)
from bounded_loops.graph.application.run_graph import GraphRunController
from bounded_loops.graph.domain.connections import ResolvedRoute
from bounded_loops.graph.domain.errors import GraphIntegrityError
from bounded_loops.graph.domain.plan import ExecutionPlan, PlannedNode, ResolvedBinding
from bounded_loops.graph.domain.pricing import ModelPrice, PriceTable
from bounded_loops.graph.domain.events import UnsignedGraphEvent
from bounded_loops.graph.domain.usage import WorkerUsage, usage_from_payload

from tests.graph.application.test_node_retry import (  # reuse the retry harness verbatim
    _DIGEST,
    _NODE_ID,
    _CrashingGate,
    _Enforcer,
    _Gate,
    _PassingVerifier,
    _events,
    _identity,
    _of_type,
    _plan,
    _policy,
)


class _MeteredWorker:
    """Reports a fixed spend on every attempt, like a provider that returns usage."""

    def __init__(self, *, input_tokens: int = 40, output_tokens: int = 60,
                 cost_microunits: int | None = None) -> None:
        self.calls = 0
        self._usage = WorkerUsage(
            input_tokens=input_tokens, output_tokens=output_tokens,
            cost_microunits=cost_microunits, reported_by="test-provider",
        )

    def execute(self, *, plan, node, envelope, attempt, repair_round=0) -> WorkerResult:  # noqa: ANN001, ARG002
        self.calls += 1
        return WorkerResult(output_artifact_digests=(_DIGEST,), usage=self._usage)


class _RoutedMeteredWorker(_MeteredWorker):
    """Reports the route it took, which the controller requires of any bound node.

    A bound node whose worker returns no route fails artifact verification — the check that
    stops a worker from silently calling a provider the immutable plan never authorised.
    """

    def execute(self, *, plan, node, envelope, attempt, repair_round=0) -> WorkerResult:  # noqa: ANN001, ARG002
        self.calls += 1
        binding = next(b for b in plan.connection_bindings if b.binding_id == node.binding_id)
        return WorkerResult(
            (_DIGEST,),
            ResolvedRoute(
                binding.provider_id, binding.model_target, binding.region,
                binding.fallback, binding.route_policy_digest,
            ),
            binding.transport,
            usage=self._usage,
        )


class _SilentWorker:
    """Reports nothing — a shell or CLI worker that genuinely cannot meter itself."""

    def __init__(self) -> None:
        self.calls = 0

    def execute(self, *, plan, node, envelope, attempt, repair_round=0) -> WorkerResult:  # noqa: ANN001, ARG002
        self.calls += 1
        return WorkerResult(output_artifact_digests=(_DIGEST,))


def _controller(
    tmp_path: Path, *, worker: object, gate: object, budgets: dict[str, object],
) -> GraphRunController:
    plan = _plan(budgets)
    return GraphRunController(
        plan=plan, event_log=GraphEventLog(tmp_path / "events.jsonl", _identity()),
        worker=worker, gate=gate, artifact_verifier=_PassingVerifier(),  # type: ignore[arg-type]
        execution_policy=_policy(plan), execution_enforcer=_Enforcer(),
        timestamp=lambda: "2026-08-12T00:00:00Z",
    )


# --------------------------------------------------------------------------------------
# The arithmetic, in isolation from any controller.
# --------------------------------------------------------------------------------------


def test_an_unmeasured_quantity_is_not_zero() -> None:
    """The single decision the whole module rests on."""
    usage = WorkerUsage(input_tokens=10, reported_by="p")

    # Output was NOT measured, so the total is unknown — not 10.
    assert usage.output_tokens is None
    assert usage.total_tokens is None
    assert WorkerUsage(input_tokens=10, output_tokens=0, reported_by="p").total_tokens == 10


def test_a_reported_number_must_name_its_source() -> None:
    """An unattributed figure cannot be audited back to a provider or an estimate."""
    with pytest.raises(GraphIntegrityError, match="what measured it"):
        WorkerUsage(input_tokens=10)

    # Nothing measured, so nothing to attribute: valid, and it serialises to nothing.
    assert WorkerUsage().payload() == {}


@pytest.mark.parametrize("field", ["input_tokens", "output_tokens", "cost_microunits", "wallclock_ms"])
def test_a_negative_charge_is_refused(field: str) -> None:
    """A negative charge would REFUND budget and buy attempts past the cap."""
    with pytest.raises(GraphIntegrityError, match="non-negative"):
        WorkerUsage(reported_by="p", **{field: -1})  # type: ignore[arg-type]


def test_a_boolean_is_not_a_token_count() -> None:
    """bool subclasses int, so True would otherwise read as one token."""
    with pytest.raises(GraphIntegrityError, match="non-negative"):
        WorkerUsage(input_tokens=True, reported_by="p")  # type: ignore[arg-type]


def test_a_zero_cost_cap_still_permits_a_free_attempt() -> None:
    """``max_cost_microunits: 0`` means "must not cost money", which free work satisfies.

    Refusing here on the arithmetic accident that 0 >= 0 would make every zero-cost node
    unrunnable the moment an operator declared the cap that describes it.
    """
    assert spend_refusal(
        spend=NodeSpend(), max_tokens=None, max_cost_microunits=0, scope="node",
    ) is None
    assert spend_refusal(
        spend=NodeSpend(cost_microunits=1, attempts_recorded=1, attempts_measured=1),
        max_tokens=None, max_cost_microunits=0, scope="node",
    ) is not None


def test_an_incomplete_total_says_so() -> None:
    """A lower bound presented as a measurement is the under-count that defeats a cap."""
    spend = NodeSpend().plus(100, None).plus(None, None)

    assert spend.tokens == 100
    assert spend.attempts_recorded == 2
    assert not spend.complete
    refusal = spend_refusal(spend=spend, max_tokens=100, max_cost_microunits=None, scope="node")
    assert refusal is not None
    assert "only some attempts" in refusal


def test_a_forged_usage_block_is_refused_on_read() -> None:
    with pytest.raises(GraphIntegrityError, match="unknown keys"):
        usage_from_payload({"input_tokens": 1, "reported_by": "p", "refund": -500})
    with pytest.raises(GraphIntegrityError, match="non-negative"):
        usage_from_payload({"cost_microunits": -500, "reported_by": "p"})


# --------------------------------------------------------------------------------------
# End to end through the controller.
# --------------------------------------------------------------------------------------


def test_every_attempt_records_what_it_spent_including_the_rejected_ones(tmp_path: Path) -> None:
    """The property retry accounting depends on: a rejected attempt still costs money."""
    worker, gate = _MeteredWorker(), _Gate(reject_first=2)

    projection = _controller(
        tmp_path, worker=worker, gate=gate, budgets={"max_attempts": 3},
    ).run()

    assert projection.state == "SUCCEEDED"
    # One spend record per EXECUTION, written before the gate ran — so the two rejected
    # attempts are measured too, not only the one that passed.
    spend_records = _of_type(tmp_path, "node.spend")
    assert [entry["attempt"] for entry in spend_records] == [1, 2, 3]
    assert all(entry["execution"] == 1 for entry in spend_records)
    assert all(entry["usage"]["input_tokens"] == 40 for entry in spend_records)
    assert all(entry["usage"]["reported_by"] == "test-provider" for entry in spend_records)
    # The outcome receipts carry no usage at all: one number, one place, no drift.
    assert all("usage" not in e for e in _of_type(tmp_path, "node.attempt.failed"))
    assert "usage" not in _of_type(tmp_path, "node.succeeded")[0]


def test_the_spend_total_comes_from_the_log_not_from_memory(tmp_path: Path) -> None:
    """Re-derived, so a total cannot drift from what a later reader computes."""
    controller = _controller(
        tmp_path, worker=_MeteredWorker(), gate=_Gate(reject_first=2),
        budgets={"max_attempts": 3},
    )
    controller.run()

    spend = consumed_spend_from(controller.plan, controller.event_log.replay())

    # Three attempts at 100 tokens each: two rejected, one accepted.
    assert spend[_NODE_ID].tokens == 300
    assert spend[_NODE_ID].attempts_recorded == 3
    assert spend[_NODE_ID].complete
    assert run_spend(spend).tokens == 300


def test_a_token_cap_stops_the_next_attempt_rather_than_the_current_one(tmp_path: Path) -> None:
    """The cap governs whether a NEW attempt may start; paid-for work is never discarded."""
    worker, gate = _MeteredWorker(), _Gate(reject_first=99)

    projection = _controller(
        tmp_path, worker=worker, gate=gate,
        # Budget for 5 attempts, but only enough tokens for 2 (100 each).
        budgets={"max_attempts": 5, "max_tokens": 150},
    ).run()

    assert projection.state == "FAILED"
    assert worker.calls == 2, "the third attempt must not start: 200 tokens already spent"
    failed = _of_type(tmp_path, "node.failed")
    assert failed[0]["cause"] == "spend_exhausted", (
        "running out of MONEY must not be reported as running out of ATTEMPTS"
    )
    assert "150" in failed[0]["reason"] and "200" in failed[0]["reason"]


def test_a_cap_is_not_walked_through_by_retrying(tmp_path: Path) -> None:
    """Without per-attempt accounting, 5 attempts under a 1-attempt cap spend 5x the cap."""
    worker = _MeteredWorker()

    _controller(
        tmp_path, worker=worker, gate=_Gate(reject_first=99),
        budgets={"max_attempts": 20, "max_tokens": 100},
    ).run()

    # One attempt reaches the cap exactly; the second is refused before it starts.
    assert worker.calls == 1
    spend = consumed_spend_from(_plan({}), GraphEventLog(tmp_path / "events.jsonl", _identity()).replay())
    assert spend[_NODE_ID].tokens == 100, "total spend must not exceed the cap by more than one attempt"


def test_a_budgeted_node_refuses_a_worker_that_cannot_meter_itself(tmp_path: Path) -> None:
    """D1: unmeasurable is refused, never metered as free.

    A cap checked against a quantity nobody reports can never fire. That is not a weaker
    guarantee than intended — it is indistinguishable from protection right up to the bill,
    which is the worst possible failure mode for a component whose entire claim is that its
    bounds are real.
    """
    worker = _SilentWorker()

    projection = _controller(
        tmp_path, worker=worker, gate=_Gate(reject_first=0),
        budgets={"max_attempts": 3, "max_tokens": 1000},
    ).run()

    assert projection.state == "FAILED"
    failed = _of_type(tmp_path, "node.failed")
    assert failed[0]["cause"] == "budget_unmeasurable"
    assert worker.calls == 1, "terminal, not retried: the wiring is what is wrong"
    assert "does not report tokens" in failed[0]["reason"]


def test_an_unbudgeted_node_runs_happily_on_a_worker_that_reports_nothing(tmp_path: Path) -> None:
    """The refusal above must be scoped to nodes that ASKED to be bounded by spend.

    Every pre-0.5 graph has no spend budget and workers that report nothing; if the
    unmeasurable rule leaked past the budgeted case it would break all of them.
    """
    projection = _controller(
        tmp_path, worker=_SilentWorker(), gate=_Gate(reject_first=0),
        budgets={"max_attempts": 3},
    ).run()

    assert projection.state == "SUCCEEDED"
    assert "usage" not in _of_type(tmp_path, "node.succeeded")[0]


def test_a_resumed_run_does_not_get_its_spend_budget_back(tmp_path: Path) -> None:
    """The load-bearing property: a budget a `kill -9` can reset is not a budget.

    An external loop that crash-restarts a run is the adversary here. If the total lived in
    the process, every restart would hand back the full budget and the cap would bound
    nothing at all — spend would be bounded only by how many times someone restarts.
    """
    budgets: dict[str, object] = {"max_attempts": 10, "max_tokens": 250}
    first = _controller(
        tmp_path, worker=_MeteredWorker(), gate=_CrashingGate(crash_on=3), budgets=budgets,
    )
    with pytest.raises(KeyboardInterrupt):
        first.run()

    # Two attempts recorded their rejections at 100 tokens each; the third was interrupted
    # mid-flight, so the run is still RUNNING and genuinely resumable.
    spent = consumed_spend_from(first.plan, first.event_log.replay())[_NODE_ID].tokens
    # Three executions are recorded, not two: the interrupted third attempt's charge is
    # written before the gate runs, so the money it spent is durable even though it never
    # reached a verdict. That is the fix for Grok's in-flight-spend CRITICAL.
    assert spent == 300
    assert first.event_log.replay_projection().state == "RUNNING"

    # A fresh controller over the SAME run directory: exactly what a crash-restart looks
    # like. Nothing of the first process survives except the receipts.
    resumed_worker = _MeteredWorker()
    resumed = _controller(
        tmp_path, worker=resumed_worker, gate=_Gate(reject_first=99), budgets=budgets,
    ).resume()

    assert resumed.state == "FAILED"
    # The cap was already reached by the recorded 300, so no further execution is bought at
    # all — the resumed worker never runs. Before the in-flight fix it ran again for free.
    assert resumed_worker.calls == 0
    failed = _of_type(tmp_path, "node.failed")
    assert failed[0]["cause"] == "spend_exhausted"
    assert consumed_spend_from(_plan({}), GraphEventLog(tmp_path / "events.jsonl", _identity()).replay())[
        _NODE_ID
    ].tokens == 300


def test_the_terminal_receipt_never_carries_usage(tmp_path: Path) -> None:
    """One attempt, one spend record. Two would double every failing attempt's charge."""
    _controller(
        tmp_path, worker=_MeteredWorker(), gate=_Gate(reject_first=99),
        budgets={"max_attempts": 2, "max_tokens": 10_000},
    ).run()

    assert all("usage" not in entry for entry in _of_type(tmp_path, "node.failed"))
    assert len(_of_type(tmp_path, "node.attempt.failed")) == 2
    spend = consumed_spend_from(_plan({}), GraphEventLog(tmp_path / "events.jsonl", _identity()).replay())
    assert spend[_NODE_ID].tokens == 200, "two attempts at 100, counted once each"
    assert len(_of_type(tmp_path, "node.spend")) == 2


def test_a_hand_forged_refund_cannot_buy_attempts_past_the_cap(tmp_path: Path) -> None:
    """A correctly re-hash-chained log must still not be able to hold a negative charge."""
    log = GraphEventLog(tmp_path / "events.jsonl", _identity())
    head = log.replay_projection().head_hash
    head = log.append(head, UnsignedGraphEvent(
        event_id="c", idempotency_key="c", event_type="run.created",
        timestamp="2026-08-12T00:00:00Z", actor="t", payload={"state": "PENDING"},
    )).event_hash

    with pytest.raises(GraphIntegrityError, match="invalid usage block"):
        log.append(head, UnsignedGraphEvent(
            event_id="f", idempotency_key="f", event_type="node.spend",
            timestamp="2026-08-12T00:00:00Z", actor="t",
            payload={
                "node_id": _NODE_ID, "attempt": 1, "execution": 1,
                "usage": {"input_tokens": -5_000, "reported_by": "forged"},
            },
        ))


def test_a_pre_0_5_receipt_without_usage_still_replays(tmp_path: Path) -> None:
    """0.4.0 run directories are durable data; adding a field must not invalidate them."""
    log = GraphEventLog(tmp_path / "events.jsonl", _identity())
    head = log.replay_projection().head_hash
    for key, event_type, payload in (
        ("c", "run.created", {"state": "PENDING"}),
        ("s", "run.started", {"state": "RUNNING"}),
    ):
        head = log.append(head, UnsignedGraphEvent(
            event_id=key, idempotency_key=key, event_type=event_type,
            timestamp="2026-08-12T00:00:00Z", actor="t", payload=payload,
        )).event_hash
    head = log.append(head, UnsignedGraphEvent(
        event_id="af", idempotency_key="af", event_type="node.attempt.failed",
        timestamp="2026-08-12T00:00:00Z", actor="t",
        payload={"node_id": _NODE_ID, "attempt": 1, "reason": "r", "cause": "worker_fault"},
    )).event_hash

    spend = consumed_spend_from(_plan({}), log.replay())

    # A 0.4.0 log has no node.spend records at all, so nothing is recorded and nothing is
    # claimed — honest, rather than a measurement of zero spend.
    assert spend[_NODE_ID].attempts_recorded == 0
    assert spend[_NODE_ID].tokens == 0


def test_spend_appears_in_the_events_a_reader_would_query(tmp_path: Path) -> None:
    """Sanity check on the durable shape itself, read back as JSON off disk."""
    _controller(
        tmp_path, worker=_MeteredWorker(cost_microunits=1_500), gate=_Gate(reject_first=1),
        budgets={"max_attempts": 2, "max_tokens": 10_000, "max_cost_microunits": 1_000_000},
    ).run()

    usages = [
        entry["payload"]["usage"] for entry in _events(tmp_path)
        if "usage" in entry.get("payload", {})
    ]
    assert len(usages) == 2
    assert [u["cost_microunits"] for u in usages] == [1_500, 1_500]
    assert json.dumps(usages)  # plain JSON: no floats, nothing exotic


class _WallclockOnlyWorker:
    """Reports elapsed time and nothing else — every subprocess and CLI worker.

    This shape is why measurability is checked per dimension. Wallclock is genuinely
    measurable by any worker, so "did it report anything?" answers yes while tokens and cost
    stay unmeasured forever.
    """

    def __init__(self) -> None:
        self.calls = 0

    def execute(self, *, plan, node, envelope, attempt, repair_round=0) -> WorkerResult:  # noqa: ANN001, ARG002
        self.calls += 1
        return WorkerResult(
            output_artifact_digests=(_DIGEST,),
            usage=WorkerUsage(wallclock_ms=1_200, reported_by="local-cli"),
        )


def test_a_worker_that_only_reports_wallclock_cannot_satisfy_a_token_cap(tmp_path: Path) -> None:
    """The dimension-blind hole: reporting SOMETHING is not reporting the capped thing.

    With a general "did the worker report usage" check this passed, ran to completion, and
    left spend.tokens at 0 with the cap never tripping — the exact silent-unenforceable
    failure the refusal exists to prevent, now reachable through an honest worker rather
    than a broken one.
    """
    worker = _WallclockOnlyWorker()

    projection = _controller(
        tmp_path, worker=worker, gate=_Gate(reject_first=0),
        budgets={"max_attempts": 3, "max_tokens": 1_000},
    ).run()

    assert projection.state == "FAILED"
    failed = _of_type(tmp_path, "node.failed")
    assert failed[0]["cause"] == "budget_unmeasurable"
    assert "tokens" in failed[0]["reason"]
    assert worker.calls == 1


def test_wallclock_alone_is_fine_when_no_spend_cap_asks_for_more(tmp_path: Path) -> None:
    """A CLI worker reporting only elapsed time is honest and must stay usable."""
    projection = _controller(
        tmp_path, worker=_WallclockOnlyWorker(), gate=_Gate(reject_first=0),
        budgets={"max_attempts": 3},
    ).run()

    assert projection.state == "SUCCEEDED"
    assert _of_type(tmp_path, "node.spend")[0]["usage"] == {
        "wallclock_ms": 1_200, "reported_by": "local-cli",
    }


def test_a_cost_cap_needs_reported_cost_not_merely_tokens(tmp_path: Path) -> None:
    """Tokens are not money until a price is applied; until then the cost cap is unmetered."""
    projection = _controller(
        tmp_path, worker=_MeteredWorker(cost_microunits=None), gate=_Gate(reject_first=0),
        budgets={"max_attempts": 2, "max_cost_microunits": 500},
    ).run()

    assert projection.state == "FAILED"
    assert _of_type(tmp_path, "node.failed")[0]["cause"] == "budget_unmeasurable"
    assert "cost" in _of_type(tmp_path, "node.failed")[0]["reason"]


@pytest.mark.parametrize(
    ("usage", "max_tokens", "max_cost", "expected"),
    [
        (None, None, None, None),
        (None, 100, None, "tokens"),
        (WorkerUsage(wallclock_ms=5, reported_by="p"), 100, None, "tokens"),
        (WorkerUsage(input_tokens=1, output_tokens=1, reported_by="p"), 100, None, None),
        (WorkerUsage(input_tokens=1, reported_by="p"), 100, None, "tokens"),
        (WorkerUsage(input_tokens=1, output_tokens=1, reported_by="p"), None, 100, "cost"),
        (WorkerUsage(cost_microunits=7, reported_by="p"), None, 100, None),
        (WorkerUsage(cost_microunits=7, reported_by="p"), 100, 100, "tokens"),
    ],
)
def test_measurability_is_decided_per_dimension(usage, max_tokens, max_cost, expected) -> None:
    from bounded_loops.graph.application.node_spend import unmeasurable_dimension

    assert unmeasurable_dimension(
        usage, max_tokens=max_tokens, max_cost_microunits=max_cost,
    ) == expected


def _routed_plan(budgets: dict[str, object]) -> ExecutionPlan:
    """A plan whose node is bound to a real provider/model, so a price table can price it."""
    base = _plan(budgets)
    node = base.nodes[0]
    routed = PlannedNode(
        node_id=node.node_id, kind=node.kind, package_digest=node.package_digest,
        binding_id="binding-1", required_effects=node.required_effects,
        isolation=node.isolation, hard_deadline_ms=node.hard_deadline_ms,
        budgets=node.budgets, approval_policy=node.approval_policy,
    )
    binding = ResolvedBinding(
        binding_id="binding-1", slot_id="slot-1", connector_id="c", connector_version="1",
        connection_id="conn-1", admission_digest="sha256:" + "9" * 64,
        route_policy_digest="sha256:" + "8" * 64, provider_id="anthropic",
        model_target="claude-opus-5", region="us", fallback=False, transport="https",
    )
    return ExecutionPlan(
        api_version=base.api_version, plan_id=base.plan_id,
        source_graph_digest=base.source_graph_digest, policy_digest=base.policy_digest,
        compiler_version=base.compiler_version, nodes=(routed,), edges=(),
        levels=((_NODE_ID,),), package_digests=base.package_digests,
        connection_bindings=(binding,), canonical_json=base.canonical_json,
    )


def _routed_policy(plan: ExecutionPlan) -> ConfiguredExecutionPolicy:
    """A bound node's envelope must carry its binding's transport, or the policy denies it."""
    return ConfiguredExecutionPolicy({
        node.node_id: ExecutionEnvelope(
            node.isolation, "https", node.required_effects, NetworkMode.DENY, (),
        )
        for node in plan.nodes
    })


def test_a_price_table_turns_tokens_into_a_cost_cap_that_works(tmp_path: Path) -> None:
    """Providers report tokens, not money, so a cost cap is unusable without a rate card.

    The estimate is recorded SEPARATELY from any provider-reported charge and names the table
    that produced it, so an operator reconciling an invoice can tell arithmetic from a bill.
    """
    plan = _routed_plan({"max_attempts": 5, "max_cost_microunits": 20_000})
    table = PriceTable(
        # $3/Mtok in, $15/Mtok out. One attempt of 1000 in + 500 out costs 3000 + 7500 =
        # 10_500 micro-USD, so the 20_000 cap affords one attempt and refuses the third.
        prices={("anthropic", "claude-opus-5"): ModelPrice(3_000_000, 15_000_000)},
        source="price-table:test-v1",
    )
    worker = _RoutedMeteredWorker(input_tokens=1_000, output_tokens=500)
    controller = GraphRunController(
        plan=plan, event_log=GraphEventLog(tmp_path / "events.jsonl", _identity()),
        worker=worker, gate=_Gate(reject_first=99), artifact_verifier=_PassingVerifier(),
        execution_policy=_routed_policy(plan), execution_enforcer=_Enforcer(),
        timestamp=lambda: "2026-08-12T00:00:00Z", price_table=table,
    )

    projection = controller.run()

    assert projection.state == "FAILED"
    # Two attempts at 10_500 reach 21_000, past the 20_000 cap, so the third never starts —
    # with two retry attempts still unspent. Money ran out, not tries.
    assert worker.calls == 2
    failed = _of_type(tmp_path, "node.failed")
    assert failed[0]["cause"] == "spend_exhausted"

    # The estimate is attributed to the table, and kept apart from cost_microunits, which
    # stays absent because this provider reported no charge of its own.
    usage = _of_type(tmp_path, "node.spend")[0]["usage"]
    assert usage["estimated_cost_microunits"] == 10_500
    assert usage["estimated_by"] == "price-table:test-v1"
    assert "cost_microunits" not in usage
    assert usage["reported_by"] == "test-provider", "the worker's own attribution survives"

    spend = consumed_spend_from(plan, controller.event_log.replay())
    assert spend[_NODE_ID].cost_microunits == 21_000


def test_a_provider_reported_charge_beats_the_tables_estimate(tmp_path: Path) -> None:
    """One is the bill, the other is arithmetic over a rate card. Both are recorded."""
    plan = _routed_plan({"max_attempts": 1, "max_cost_microunits": 1_000_000})
    controller = GraphRunController(
        plan=plan, event_log=GraphEventLog(tmp_path / "events.jsonl", _identity()),
        worker=_RoutedMeteredWorker(input_tokens=1_000, output_tokens=500, cost_microunits=9_999),
        gate=_Gate(reject_first=0), artifact_verifier=_PassingVerifier(),
        execution_policy=_routed_policy(plan), execution_enforcer=_Enforcer(),
        timestamp=lambda: "2026-08-12T00:00:00Z",
        price_table=PriceTable(
            prices={("anthropic", "claude-opus-5"): ModelPrice(3_000_000, 15_000_000)},
            source="price-table:test-v1",
        ),
    )

    assert controller.run().state == "SUCCEEDED"

    usage = _of_type(tmp_path, "node.spend")[0]["usage"]
    assert usage["cost_microunits"] == 9_999
    # No estimate is even computed once the provider has billed us: the worker's own report is
    # not second-guessed, and an estimate beside a bill would only invite the wrong one to be
    # summed.
    assert "estimated_cost_microunits" not in usage
    assert consumed_spend_from(plan, controller.event_log.replay())[_NODE_ID].cost_microunits == 9_999


def test_an_unpriced_route_makes_a_cost_cap_fail_closed(tmp_path: Path) -> None:
    """No default prices ship, so out of the box every cost cap refuses rather than guesses."""
    projection = _controller(
        tmp_path, worker=_MeteredWorker(input_tokens=1_000, output_tokens=500),
        gate=_Gate(reject_first=0), budgets={"max_attempts": 2, "max_cost_microunits": 20_000},
    ).run()

    assert projection.state == "FAILED"
    failed = _of_type(tmp_path, "node.failed")
    assert failed[0]["cause"] == "budget_unmeasurable"
    assert "cost" in failed[0]["reason"]


# --------------------------------------------------------------------------------------
# The run-level total: exhausting it PAUSES rather than fails.
# --------------------------------------------------------------------------------------


def _budgeted_run(
    tmp_path: Path, *, worker: object, gate: object, run_budget: RunBudget,
    budgets: dict[str, object],
) -> GraphRunController:
    plan = _plan(budgets)
    return GraphRunController(
        plan=plan, event_log=GraphEventLog(tmp_path / "events.jsonl", _identity()),
        worker=worker, gate=gate, artifact_verifier=_PassingVerifier(),  # type: ignore[arg-type]
        execution_policy=_policy(plan), execution_enforcer=_Enforcer(),
        timestamp=lambda: "2026-08-12T00:00:00Z", run_budget=run_budget,
    )


def test_reaching_the_run_total_pauses_the_run_instead_of_failing_it(tmp_path: Path) -> None:
    """The operator set this number, so reaching it stops and asks rather than discards work.

    A node's own cap failing that node is proportionate — one step overran its allowance. A
    run total is the operator's call, and failing the run would throw away everything already
    completed and paid for.
    """
    worker = _MeteredWorker()  # 100 tokens per attempt
    controller = _budgeted_run(
        tmp_path, worker=worker, gate=_Gate(reject_first=99),
        run_budget=RunBudget(max_tokens=150), budgets={"max_attempts": 10},
    )

    projection = controller.run()

    # RUNNING, not FAILED: the run is still resumable, which is the entire difference between
    # pausing for a decision and giving up.
    assert projection.state == "RUNNING"
    assert worker.calls == 2, "the third attempt must not start: 200 of 150 tokens spent"
    paused = _of_type(tmp_path, "run.budget.paused")
    assert len(paused) == 1
    assert paused[0]["tokens"] == 200
    assert paused[0]["max_tokens"] == 150
    assert "this run" in paused[0]["reason"]
    assert not _of_type(tmp_path, "node.failed"), "a pause is not a failure"


def test_resuming_with_the_same_ceiling_pauses_again_without_growing_the_log(
    tmp_path: Path,
) -> None:
    """Polling a paused run must be free. The pause key names where it happened, so the
    identical event de-duplicates instead of appending once per poll."""
    controller = _budgeted_run(
        tmp_path, worker=_MeteredWorker(), gate=_Gate(reject_first=99),
        run_budget=RunBudget(max_tokens=150), budgets={"max_attempts": 10},
    )
    controller.run()
    events_after_pause = len(_events(tmp_path))

    for _ in range(3):
        resumed = _budgeted_run(
            tmp_path, worker=_MeteredWorker(), gate=_Gate(reject_first=99),
            run_budget=RunBudget(max_tokens=150), budgets={"max_attempts": 10},
        ).resume()
        assert resumed.state == "RUNNING"

    assert len(_of_type(tmp_path, "run.budget.paused")) == 1
    # One event for the first poll, and nothing at all after that. Two rules compose to give
    # this: the pause re-appends an identical record that de-duplicates, which leaves
    # run.resumed as the last event, which is exactly the condition under which the next
    # resume skips recording itself. So a client can poll a budget-paused run forever without
    # touching the log — while a resume that actually advances work is still recorded.
    growth = len(_events(tmp_path)) - events_after_pause
    assert growth == 1, f"polling a paused run must be free; it appended {growth} events"
    assert [p["resume_ordinal"] for p in _of_type(tmp_path, "run.resumed")] == [1]


def test_resuming_with_a_raised_ceiling_continues_the_run(tmp_path: Path) -> None:
    """Continuing costs the operator an explicit new number — never a bypass grant.

    A grant recorded in the log could be replayed to buy spend a second time. A ceiling passed
    in at resume cannot: it is not in the log, it is the operator's decision for this attempt
    at continuing, and the receipts show exactly what was spent under it.
    """
    controller = _budgeted_run(
        tmp_path, worker=_MeteredWorker(), gate=_Gate(reject_first=1),
        run_budget=RunBudget(max_tokens=50), budgets={"max_attempts": 10},
    )
    assert controller.run().state == "RUNNING"
    assert _of_type(tmp_path, "run.budget.paused")[0]["tokens"] == 100

    resumed = _budgeted_run(
        tmp_path, worker=_MeteredWorker(), gate=_Gate(reject_first=0),
        run_budget=RunBudget(max_tokens=100_000), budgets={"max_attempts": 10},
    ).resume()

    assert resumed.state == "SUCCEEDED"


def test_a_run_with_no_declared_total_is_never_paused(tmp_path: Path) -> None:
    """Every pre-0.5 run has no run budget; the pause must be invisible to them."""
    projection = _budgeted_run(
        tmp_path, worker=_MeteredWorker(), gate=_Gate(reject_first=2),
        run_budget=RunBudget(), budgets={"max_attempts": 5},
    ).run()

    assert projection.state == "SUCCEEDED"
    assert not _of_type(tmp_path, "run.budget.paused")


def test_a_pause_never_parks_the_node_on_awaiting_approval(tmp_path: Path) -> None:
    """AWAITING_APPROVAL means a decision recorded against an approval node, with a resolver.

    None of that exists for a budget pause, so claiming it would be a lie in the Arena. It is
    also unreachable in general — a resumed node sits in STARTING, RUNNING or GATING, none of
    which has an AWAITING_APPROVAL edge, and adding one would make
    RUNNING -> AWAITING_APPROVAL -> SUCCEEDED reachable, letting a node succeed on a human
    decision instead of on its independent gate.
    """
    controller = _budgeted_run(
        tmp_path, worker=_MeteredWorker(), gate=_Gate(reject_first=99),
        run_budget=RunBudget(max_tokens=150), budgets={"max_attempts": 10},
    )
    controller.run()

    assert not _of_type(tmp_path, "node.awaiting_approval")
    # And the run is still readable by the Arena and the resume path, which is what an
    # illegal lifecycle transition would have destroyed.
    states = latest_node_states(controller.plan, controller.event_log.replay())
    assert states[_NODE_ID]["state"] in ("RUNNING", "GATING")


def test_a_pause_records_the_ceiling_it_reached(tmp_path: Path) -> None:
    """A pause the operator cannot check against what they authorised explains nothing."""
    _budgeted_run(
        tmp_path, worker=_MeteredWorker(cost_microunits=900), gate=_Gate(reject_first=99),
        run_budget=RunBudget(max_cost_microunits=1_000), budgets={"max_attempts": 5},
    ).run()

    paused = _of_type(tmp_path, "run.budget.paused")[0]
    assert paused["max_cost_microunits"] == 1_000
    assert paused["cost_microunits"] == 1_800
    assert "cost" in paused["reason"]


@pytest.mark.parametrize("field", ["max_tokens", "max_cost_microunits"])
def test_a_negative_run_ceiling_is_refused(field: str) -> None:
    with pytest.raises(GraphIntegrityError, match="non-negative"):
        RunBudget(**{field: -1})  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------
# What an operator actually sees.
# --------------------------------------------------------------------------------------


class _AlwaysAuthorized:
    def authorize(self, request: object) -> bool:  # noqa: ARG002
        return True


class UnverifiedReceiptReader:
    def verify(self, identity: object, receipts: object) -> None:  # noqa: ARG002
        return None


def _projection(controller: GraphRunController) -> object:
    from bounded_loops.graph.application.arena_projection import (
        ArenaReadRequest, read_arena_projection,
    )

    return read_arena_projection(
        controller.plan, controller.event_log,
        ArenaReadRequest(
            subject_id="operator", organization_id="org-1", project_id="project-1",
            run_id="graph-run-1",
        ),
        _AlwaysAuthorized(), UnverifiedReceiptReader(),
    )


def test_the_arena_reports_spend_per_node_and_for_the_run(tmp_path: Path) -> None:
    """Enforced but invisible is not good enough: an operator has to see where it went."""
    controller = _budgeted_run(
        tmp_path, worker=_MeteredWorker(cost_microunits=1_200), gate=_Gate(reject_first=99),
        run_budget=RunBudget(max_tokens=150), budgets={"max_attempts": 10},
    )
    controller.run()

    projection = _projection(controller)

    assert projection.spend_tokens == 200  # type: ignore[attr-defined]
    assert projection.spend_cost_microunits == 2_400  # type: ignore[attr-defined]
    assert projection.spend_complete is True  # type: ignore[attr-defined]
    assert projection.nodes[0].spend_tokens == 200  # type: ignore[attr-defined]
    assert projection.budget_pause is not None  # type: ignore[attr-defined]


def test_a_budget_paused_run_does_not_render_as_making_progress(tmp_path: Path) -> None:
    """Without this the operator's own surface says "Running: worker-node".

    Nothing is running and nothing will until they act. A pause that is indistinguishable from
    progress is the one failure the pause exists to avoid — it is worse than a plain failure,
    because a failure at least tells them to look.
    """
    from bounded_loops.graph.application.state_document import render_state_markdown

    controller = _budgeted_run(
        tmp_path, worker=_MeteredWorker(), gate=_Gate(reject_first=99),
        run_budget=RunBudget(max_tokens=150), budgets={"max_attempts": 10},
    )
    controller.run()

    document = render_state_markdown(_projection(controller))  # type: ignore[arg-type]

    assert "Paused on budget" in document
    assert "Running:" not in document
    assert "--max-tokens" in document, "the operator is told how to continue"
    assert "**200** tokens" in document


def test_an_incomplete_spend_total_is_labelled_as_a_floor(tmp_path: Path) -> None:
    """"1,200 tokens" and "at least 1,200 tokens" support different decisions."""
    from bounded_loops.graph.application.state_document import render_state_markdown

    controller = _budgeted_run(
        # A worker that raises leaves an attempt recorded with no usage, so the total becomes a
        # lower bound rather than a measurement.
        tmp_path, worker=_SometimesRaisingMeteredWorker(), gate=_Gate(reject_first=99),
        run_budget=RunBudget(), budgets={"max_attempts": 3},
    )
    controller.run()

    projection = _projection(controller)
    assert projection.spend_complete is False  # type: ignore[attr-defined]
    assert "at least" in render_state_markdown(projection)  # type: ignore[arg-type]


class _SometimesRaisingMeteredWorker(_MeteredWorker):
    """Reports usage, then crashes — an attempt that spent money it could not report."""

    def execute(self, *, plan, node, envelope, attempt, repair_round=0) -> WorkerResult:  # noqa: ANN001, ARG002
        self.calls += 1
        if self.calls == 1:
            return WorkerResult(output_artifact_digests=(_DIGEST,), usage=self._usage)
        raise RuntimeError("crashed before it could measure itself")


# --------------------------------------------------------------------------------------
# Audit round 1 (Muse + Grok). Each test is the probe that proved the finding.
# --------------------------------------------------------------------------------------


def test_raising_the_ceiling_too_little_does_not_wedge_the_run(tmp_path: Path) -> None:
    """A1 — Muse MAJOR, proved. The most ordinary path there is, and it destroyed the run.

    An operator raises the ceiling a little, it is still below what was already spent, and the
    second pause re-used the first pause's key with a different payload — which the log refuses
    outright ("idempotency key was reused with a different event"). The run then could not be
    resumed AT ALL, which is the exact opposite of what a pause is for.
    """
    budgets: dict[str, object] = {"max_attempts": 10}
    controller = _budgeted_run(
        tmp_path, worker=_MeteredWorker(), gate=_Gate(reject_first=99),
        run_budget=RunBudget(max_tokens=150), budgets=budgets,
    )
    assert controller.run().state == "RUNNING"

    # Each raise is still below the 200 already spent, so each pauses again — and each is a
    # distinct authorised ceiling, so each gets its own record rather than colliding.
    for ceiling in (151, 152, 199):
        resumed = _budgeted_run(
            tmp_path, worker=_MeteredWorker(), gate=_Gate(reject_first=99),
            run_budget=RunBudget(max_tokens=ceiling), budgets=budgets,
        ).resume()
        assert resumed.state == "RUNNING", f"ceiling {ceiling} wedged the run"

    ceilings = [entry["max_tokens"] for entry in _of_type(tmp_path, "run.budget.paused")]
    assert ceilings == [150, 151, 152, 199]
    # And an identical pause still de-duplicates, so polling one ceiling stays free.
    _budgeted_run(
        tmp_path, worker=_MeteredWorker(), gate=_Gate(reject_first=99),
        run_budget=RunBudget(max_tokens=199), budgets=budgets,
    ).resume()
    assert len(_of_type(tmp_path, "run.budget.paused")) == 4


def test_one_attempt_cannot_carry_two_outcomes(tmp_path: Path) -> None:
    """A2 — Muse CRITICAL, proved. 200 tokens spent read as 400, and complete said "exact".

    An honest controller cannot produce both receipts: recording a failure marks the attempt
    spent, so the next takes a new number. But a hash-valid log can carry both — the two use
    different idempotency keys, and node.attempt.failed is additive so the lifecycle validation
    never sees it. The charge was then summed twice while the total still reported itself as a
    measurement. A doubled number wearing the face of a measurement is worse than an obvious
    gap, because it gets acted on.
    """
    log = GraphEventLog(tmp_path / "events.jsonl", _identity())
    head = log.replay_projection().head_hash
    for key, event_type, payload in (
        ("c", "run.created", {"state": "PENDING"}),
        ("s", "run.started", {"state": "RUNNING"}),
        ("r", "node.ready", {"node_id": _NODE_ID, "state": "READY", "attempt": 1}),
        ("st", "node.starting", {"node_id": _NODE_ID, "state": "STARTING", "attempt": 1}),
        ("ru", "node.running", {"node_id": _NODE_ID, "state": "RUNNING", "attempt": 1}),
        ("g", "node.gating", {"node_id": _NODE_ID, "state": "GATING", "attempt": 1}),
        ("af", "node.attempt.failed", {
            "node_id": _NODE_ID, "attempt": 1, "reason": "r", "cause": "worker_fault",
        }),
        ("su", "node.succeeded", {
            "node_id": _NODE_ID, "state": "SUCCEEDED", "attempt": 1,
            "artifact_digests": [_DIGEST],
        }),
    ):
        head = log.append(head, UnsignedGraphEvent(
            event_id=key, idempotency_key=key, event_type=event_type,
            timestamp="2026-08-12T00:00:00Z", actor="test", payload=payload,
        )).event_hash

    with pytest.raises(GraphIntegrityError, match="two outcomes"):
        consumed_spend_from(_plan({}), log.replay())


def test_a_run_ceiling_also_requires_the_spend_to_be_measurable(tmp_path: Path) -> None:
    """A3 — Grok hypothesis, proved. The unmeasurable rule was applied only to NODE caps.

    A silent worker under a 10-token RUN ceiling ran all 20 of its attempts while the run total
    sat at 0 and the ceiling never tripped. Exactly the same hole as the dimension-blind check,
    one level up: a cap checked against a quantity nobody reports can never fire, and that is
    indistinguishable from protection.
    """
    worker = _SilentWorker()

    projection = _budgeted_run(
        tmp_path, worker=worker, gate=_Gate(reject_first=99),
        run_budget=RunBudget(max_tokens=10), budgets={"max_attempts": 20},
    ).run()

    assert projection.state == "FAILED"
    assert worker.calls == 1, "must refuse on the first attempt, not run all 20"
    failed = _of_type(tmp_path, "node.failed")
    assert failed[0]["cause"] == "budget_unmeasurable"
    assert "tokens" in failed[0]["reason"]


def test_a_paused_ceiling_is_sticky_when_a_continuation_names_no_number(tmp_path: Path) -> None:
    """A3/F5 — resuming with NO ceiling once meant unbounded: 2000 tokens against 150 authorised.

    Refusing that was the first fix and it wedged every surface that continues a run without
    typing a number — `bl graph approve`, the console's approve button, MCP's resume. Sticky is
    better than refusing: the continuation is bounded by the number the operator already
    authorised, so it pauses again immediately rather than spending freely OR becoming
    unresumable. Carrying a ceiling forward can only ADD a bound, never relax one.
    """
    budgets: dict[str, object] = {"max_attempts": 20}
    controller = _budgeted_run(
        tmp_path, worker=_MeteredWorker(), gate=_Gate(reject_first=99),
        run_budget=RunBudget(max_tokens=150), budgets=budgets,
    )
    controller.run()
    at_pause = run_spend(consumed_spend_from(controller.plan, controller.event_log.replay()))

    worker = _MeteredWorker()
    resumed = _budgeted_run(
        tmp_path, worker=worker, gate=_Gate(reject_first=99),
        run_budget=RunBudget(), budgets=budgets,
    ).resume()

    assert resumed.state == "RUNNING", "still resumable — not wedged"
    assert worker.calls == 0, "and not one further execution was bought"
    after = run_spend(consumed_spend_from(controller.plan, controller.event_log.replay()))
    assert after.tokens == at_pause.tokens == 200


def test_a_token_only_continuation_does_not_drop_a_cost_ceiling(tmp_path: Path) -> None:
    """F7 — Grok round 2, proved. Raising tokens silently removed the cost cap.

    A run paused on cost at 2000 of an authorised 1500, continued with only a token ceiling, ran
    on to a recorded cost of 4000. The operator never typed a higher cost ceiling; the dimension
    they did not mention was read as "unbounded".
    """
    budgets: dict[str, object] = {"max_attempts": 10}
    controller = _budgeted_run(
        tmp_path, worker=_MeteredWorker(cost_microunits=1_000), gate=_Gate(reject_first=99),
        run_budget=RunBudget(max_cost_microunits=1_500), budgets=budgets,
    )
    controller.run()

    worker = _MeteredWorker(cost_microunits=1_000)
    resumed = _budgeted_run(
        tmp_path, worker=worker, gate=_Gate(reject_first=99),
        run_budget=RunBudget(max_tokens=10_000), budgets=budgets,
    ).resume()

    assert resumed.state == "RUNNING"
    assert worker.calls == 0, "the cost ceiling the operator never touched still binds"
    total = run_spend(consumed_spend_from(controller.plan, controller.event_log.replay()))
    assert total.cost_microunits == 2_000, "not the 4000 an unbounded cost dimension reached"


def test_a_re_driven_attempt_records_its_second_payment(tmp_path: Path) -> None:
    """F1 — Grok round 2, proved. The execution ordinal did not advance on the first re-drive.

    An at-least-once resume pays the provider again. Both executions keyed ``execution=1``, so
    with different usage the log refused the second outright (run unresumable) and with identical
    usage the second real payment was silently not recorded while the total still called itself
    exact. My own resume test never reached this: it exhausted the NODE cap first, so no further
    execution was ever bought.
    """
    budgets: dict[str, object] = {"max_attempts": 3}
    controller = _budgeted_run(
        tmp_path, worker=_MeteredWorker(), gate=_CrashingGate(crash_on=1),
        run_budget=RunBudget(max_tokens=100_000), budgets=budgets,
    )
    with pytest.raises(KeyboardInterrupt):
        controller.run()
    assert run_spend(consumed_spend_from(controller.plan, controller.event_log.replay())).tokens == 100

    # The re-drive pays again, and reports a DIFFERENT amount — which is what collided.
    resumed = _budgeted_run(
        tmp_path, worker=_MeteredWorker(input_tokens=100, output_tokens=150),
        gate=_Gate(reject_first=0), budgets=budgets,
        run_budget=RunBudget(max_tokens=100_000),
    ).resume()

    assert resumed.state == "SUCCEEDED", "a colliding key made this unresumable"
    records = _of_type(tmp_path, "node.spend")
    assert [(r["attempt"], r["execution"]) for r in records] == [(1, 1), (1, 2)]
    total = run_spend(consumed_spend_from(controller.plan, controller.event_log.replay()))
    assert total.tokens == 350, "both payments recorded: 100 then 250"


def test_a_run_that_never_paused_needs_no_ceiling_to_resume(tmp_path: Path) -> None:
    """The refusal above must be scoped to runs that actually hit a ceiling.

    Every pre-0.5 run has no budget at all; if the guard leaked past the paused case it would
    make all of them unresumable.
    """
    controller = _budgeted_run(
        tmp_path, worker=_MeteredWorker(), gate=_CrashingGate(crash_on=2),
        run_budget=RunBudget(), budgets={"max_attempts": 3},
    )
    with pytest.raises(KeyboardInterrupt):
        controller.run()

    resumed = _budgeted_run(
        tmp_path, worker=_MeteredWorker(), gate=_Gate(reject_first=0),
        run_budget=RunBudget(), budgets={"max_attempts": 3},
    ).resume()

    assert resumed.state == "SUCCEEDED"


def test_a_finished_run_stays_readable_without_a_ceiling(tmp_path: Path) -> None:
    """The ceiling guard must not fire on a run that paused, was raised, and COMPLETED.

    Nothing more can be spent by a terminal run, so refusing to read it would be pure noise —
    and it would break `status`-style idempotent reads of any run that ever paused. Correct by
    construction rather than by a special case: resume() returns every terminal state before
    the guard is reached. Pinned here so that ordering cannot drift.
    """
    budgets: dict[str, object] = {"max_attempts": 10}
    controller = _budgeted_run(
        tmp_path, worker=_MeteredWorker(), gate=_Gate(reject_first=2),
        run_budget=RunBudget(max_tokens=150), budgets=budgets,
    )
    assert controller.run().state == "RUNNING"
    assert _budgeted_run(
        tmp_path, worker=_MeteredWorker(), gate=_Gate(reject_first=0),
        run_budget=RunBudget(max_tokens=100_000), budgets=budgets,
    ).resume().state == "SUCCEEDED"

    # Now with no ceiling at all: an idempotent read, not a refusal.
    assert _budgeted_run(
        tmp_path, worker=_MeteredWorker(), gate=_Gate(reject_first=0),
        run_budget=RunBudget(), budgets=budgets,
    ).resume().state == "SUCCEEDED"


def test_a_reported_zero_token_count_is_not_a_measurement(tmp_path: Path) -> None:
    """Grok MAJOR, proved. A worker reporting 0/0 made every token cap a no-op.

    Ten calls ran under a cap of 1, SUCCEEDED, with the total at 0 and complete claiming it
    exact. A real model call cannot consume zero input tokens — the prompt itself is input — so
    a reported 0 means the metering is not trustworthy, whether the provider is broken or lying.
    """
    worker = _MeteredWorker(input_tokens=0, output_tokens=0)

    projection = _budgeted_run(
        tmp_path, worker=worker, gate=_Gate(reject_first=9),
        run_budget=RunBudget(max_tokens=1), budgets={"max_attempts": 10},
    ).run()

    assert projection.state == "FAILED"
    assert worker.calls == 1
    assert _of_type(tmp_path, "node.failed")[0]["cause"] == "budget_unmeasurable"


def test_a_zero_cost_is_still_a_real_measurement(tmp_path: Path) -> None:
    """Cost is deliberately NOT treated like tokens: a charge of zero is entirely credible.

    A free tier, a local model, a zero-priced route. Refusing it would break the
    ``max_cost_microunits: 0`` case that exists precisely to permit free work.
    """
    projection = _budgeted_run(
        tmp_path, worker=_MeteredWorker(cost_microunits=0), gate=_Gate(reject_first=0),
        run_budget=RunBudget(max_cost_microunits=0), budgets={"max_attempts": 3},
    ).run()

    assert projection.state == "SUCCEEDED"


class _BrokenContractWorker:
    """A worker whose contract cannot be honoured — a CLI whose envelope we cannot read."""

    def __init__(self) -> None:
        self.calls = 0

    def execute(self, *, plan, node, envelope, attempt, repair_round=0) -> WorkerResult:  # noqa: ANN001, ARG002
        from bounded_loops.graph.domain.errors import WorkerContractError

        self.calls += 1
        raise WorkerContractError("the CLI returned an envelope this version cannot read")


def test_a_broken_worker_contract_ends_the_node_instead_of_paying_per_retry(
    tmp_path: Path,
) -> None:
    """Grok round 3, P1 — and the second time this phase that a closed door spent more.

    An unreadable CLI envelope was raised as a plain Exception, so the controller recorded an
    unmeasured spend and RETRIED it as a worker fault: identical failure on every attempt, the
    provider paid each time, and the declared cap never consulted because nothing measurable
    accumulated. A deterministic contract failure ends the node on its first attempt.
    """
    worker = _BrokenContractWorker()

    projection = _budgeted_run(
        tmp_path, worker=worker, gate=_Gate(reject_first=0),
        run_budget=RunBudget(max_tokens=100_000), budgets={"max_attempts": 5},
    ).run()

    assert projection.state == "FAILED"
    assert worker.calls == 1, "five attempts would have been five payments"
    failed = _of_type(tmp_path, "node.failed")
    assert failed[0]["cause"] == "worker_contract"
    # The execution is still recorded with no usage: it RAN, and may well have been billed, so
    # the run total stays a lower bound rather than silently omitting a paid call.
    assert len(_of_type(tmp_path, "node.spend")) == 1
    assert "usage" not in _of_type(tmp_path, "node.spend")[0]


def test_a_transient_worker_fault_is_still_retried(tmp_path: Path) -> None:
    """The distinction is the whole point: a fault is worth retrying, a broken contract is not."""
    from tests.graph.application.test_node_retry import _RaisingWorker

    worker = _RaisingWorker()

    projection = _budgeted_run(
        tmp_path, worker=worker, gate=_Gate(reject_first=0),
        run_budget=RunBudget(), budgets={"max_attempts": 3},
    ).run()

    assert projection.state == "FAILED"
    assert worker.calls == 3, "a transient fault gets its whole retry budget"
    assert _of_type(tmp_path, "node.failed")[0]["cause"] == "worker_fault"
