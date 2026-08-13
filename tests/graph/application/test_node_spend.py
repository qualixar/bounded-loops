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
from bounded_loops.graph.application.node_contracts import WorkerResult
from bounded_loops.graph.application.node_spend import (
    NodeSpend,
    consumed_spend_from,
    run_spend,
    spend_refusal,
)
from bounded_loops.graph.application.run_graph import GraphRunController
from bounded_loops.graph.domain.errors import GraphIntegrityError
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

    def execute(self, *, plan, node, envelope, attempt) -> WorkerResult:  # noqa: ANN001, ARG002
        self.calls += 1
        return WorkerResult(output_artifact_digests=(_DIGEST,), usage=self._usage)


class _SilentWorker:
    """Reports nothing — a shell or CLI worker that genuinely cannot meter itself."""

    def __init__(self) -> None:
        self.calls = 0

    def execute(self, *, plan, node, envelope, attempt) -> WorkerResult:  # noqa: ANN001, ARG002
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
    rejected = _of_type(tmp_path, "node.attempt.failed")
    assert [entry["usage"]["input_tokens"] for entry in rejected] == [40, 40]
    assert all(entry["usage"]["reported_by"] == "test-provider" for entry in rejected)
    accepted = _of_type(tmp_path, "node.succeeded")
    assert accepted[0]["usage"] == {
        "input_tokens": 40, "output_tokens": 60, "reported_by": "test-provider",
    }


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
    assert spent == 200
    assert first.event_log.replay_projection().state == "RUNNING"

    # A fresh controller over the SAME run directory: exactly what a crash-restart looks
    # like. Nothing of the first process survives except the receipts.
    resumed_worker = _MeteredWorker()
    resumed = _controller(
        tmp_path, worker=resumed_worker, gate=_Gate(reject_first=99), budgets=budgets,
    ).resume()

    assert resumed.state == "FAILED"
    # The interrupted attempt 3 is re-driven (at-least-once), reaching 300 tokens — and then
    # the cap stops attempt 4, with 7 retry attempts still unspent. Money ran out, not tries.
    assert resumed_worker.calls == 1
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
            event_id="f", idempotency_key="f", event_type="node.attempt.failed",
            timestamp="2026-08-12T00:00:00Z", actor="t",
            payload={
                "node_id": _NODE_ID, "attempt": 1, "reason": "r", "cause": "worker_fault",
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

    assert spend[_NODE_ID].attempts_recorded == 1
    assert spend[_NODE_ID].tokens == 0
    assert not spend[_NODE_ID].complete, (
        "an attempt that reported nothing must not read as an attempt that spent nothing"
    )


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

    def execute(self, *, plan, node, envelope, attempt) -> WorkerResult:  # noqa: ANN001, ARG002
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
    assert _of_type(tmp_path, "node.succeeded")[0]["usage"] == {
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
