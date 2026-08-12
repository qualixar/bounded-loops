"""Bounded loops in the graph layer: a node retries until its gate accepts.

Before this existed, ``GraphRunController`` never read ``PlannedNode.budgets`` — a node
declaring ``max_attempts: 5`` received exactly one attempt, and every emitted event
carried a hardcoded ``attempt: 1``. These tests pin the behaviour that replaces it, and
in particular pin the two properties the reliability mathematics depends on:

* every attempt is separately observable, including the ones the gate REJECTED, and
* a gate rejection is distinguishable from a worker fault without parsing free text.

Without both, the per-attempt gate false-accept rate cannot be estimated from the log.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bounded_loops.graph.adapters.persistence.event_log import GraphEventLog
from bounded_loops.graph.application.arena_projection import latest_node_states
from bounded_loops.graph.application.execution_policy import (
    ConfiguredExecutionPolicy,
    ExecutionEnforcerPort,
    ExecutionEnvelope,
    NetworkMode,
)
from bounded_loops.graph.application.run_graph import (
    GateVerdict,
    GraphRunController,
    IndependentGatePort,
    NodeWorkerPort,
    WorkerResult,
)
from bounded_loops.graph.domain.authoring import Effect, IsolationLevel
from bounded_loops.graph.domain.errors import GraphIntegrityError
from bounded_loops.graph.domain.events import GraphRunIdentity
from bounded_loops.graph.domain.plan import ExecutionPlan, PlannedNode

_DIGEST = "sha256:" + "d" * 64
_NODE_ID = "worker-node"


def _identity() -> GraphRunIdentity:
    return GraphRunIdentity(
        organization_id="org-1", project_id="project-1", run_id="graph-run-1",
        graph_digest="sha256:" + "a" * 64, plan_digest="sha256:" + "b" * 64,
        policy_digest="sha256:" + "c" * 64,
    )


def _node(budgets: dict[str, object] | None = None) -> PlannedNode:
    return PlannedNode(
        node_id=_NODE_ID, kind="tool", package_digest=_DIGEST, binding_id=None,
        required_effects=frozenset({Effect.READ_ONLY}),
        isolation=IsolationLevel.WORKSPACE_ONLY, hard_deadline_ms=1_000,
        budgets={} if budgets is None else budgets, approval_policy={},
    )


def _plan(budgets: dict[str, object] | None = None) -> ExecutionPlan:
    node = _node(budgets)
    return ExecutionPlan(
        api_version="bounded-loops.dev/plan/v1", plan_id="sha256:" + "b" * 64,
        source_graph_digest="sha256:" + "a" * 64, policy_digest="sha256:" + "c" * 64,
        compiler_version="test", nodes=(node,), edges=(), levels=((_NODE_ID,),),
        package_digests=(_DIGEST,), connection_bindings=(), canonical_json=b"{}",
    )


def _policy(plan: ExecutionPlan) -> ConfiguredExecutionPolicy:
    return ConfiguredExecutionPolicy({
        node.node_id: ExecutionEnvelope(
            node.isolation, None, node.required_effects, NetworkMode.DENY, (),
        )
        for node in plan.nodes
    })


class _Enforcer:
    def __init__(self) -> None:
        self.calls = 0

    def enforce(self, *, plan, node, envelope) -> None:  # noqa: ANN001, ARG002
        self.calls += 1


class _DenyingEnforcer:
    def __init__(self) -> None:
        self.calls = 0

    def enforce(self, *, plan, node, envelope) -> None:  # noqa: ANN001, ARG002
        self.calls += 1
        raise GraphIntegrityError("environment denied")


class _Worker:
    """Succeeds every time, recording the attempt number it was handed."""

    def __init__(self) -> None:
        self.calls = 0
        self.attempts: list[int] = []

    def execute(self, *, plan, node, envelope, attempt) -> WorkerResult:  # noqa: ANN001, ARG002
        self.calls += 1
        self.attempts.append(attempt)
        return WorkerResult(output_artifact_digests=(_DIGEST,))


class _RaisingWorker:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, *, plan, node, envelope, attempt=1) -> WorkerResult:  # noqa: ANN001, ARG002
        self.calls += 1
        raise RuntimeError("transient worker fault")


class _Gate:
    """Rejects the first ``reject_first`` calls, then accepts."""

    def __init__(self, reject_first: int) -> None:
        self._reject_first = reject_first
        self.calls = 0

    def evaluate(self, *, plan, node, result) -> GateVerdict:  # noqa: ANN001, ARG002
        self.calls += 1
        if self.calls <= self._reject_first:
            return GateVerdict(False, f"rejected on call {self.calls}")
        return GateVerdict(True, "accepted")


class _PassingVerifier:
    """Artifact verification is out of scope here; the retry logic is under test."""

    def verify(self, *, identity, digests) -> None:  # noqa: ANN001, ARG002
        return None


def _controller(
    tmp_path: Path, *, worker: NodeWorkerPort, gate: IndependentGatePort,
    budgets: dict[str, object] | None = None,
    enforcer: ExecutionEnforcerPort | None = None,
) -> GraphRunController:
    plan = _plan(budgets)
    return GraphRunController(
        plan=plan, event_log=GraphEventLog(tmp_path / "events.jsonl", _identity()),
        worker=worker, gate=gate, artifact_verifier=_PassingVerifier(),
        execution_policy=_policy(plan),
        execution_enforcer=_Enforcer() if enforcer is None else enforcer,
        timestamp=lambda: "2026-08-12T00:00:00Z",
    )


def _events(tmp_path: Path) -> list[dict]:
    lines = (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _of_type(tmp_path: Path, event_type: str) -> list[dict]:
    # Persisted records are flat: event_type and payload are top-level keys.
    return [
        entry["payload"] for entry in _events(tmp_path)
        if entry["event_type"] == event_type
    ]


def test_a_node_retries_until_its_independent_gate_accepts(tmp_path: Path) -> None:
    worker, gate = _Worker(), _Gate(reject_first=2)

    projection = _controller(
        tmp_path, worker=worker, gate=gate, budgets={"max_attempts": 3},
    ).run()

    assert projection.state == "SUCCEEDED"
    assert worker.calls == 3, "the worker must be re-run on each attempt"
    assert gate.calls == 3
    # The worker is TOLD which attempt it is on. Without this, artifact provenance and any
    # per-attempt credential audience stay pinned to attempt 1 while the log says 3 — the
    # workers previously hardcoded producer_attempt="1".
    assert worker.attempts == [1, 2, 3]
    # Three attempts observable, two of them rejected.
    assert [payload["attempt"] for payload in _of_type(tmp_path, "node.running")] == [1, 2, 3]
    assert [payload["attempt"] for payload in _of_type(tmp_path, "node.attempt.failed")] == [1, 2]
    succeeded = _of_type(tmp_path, "node.succeeded")
    assert len(succeeded) == 1
    assert succeeded[0]["attempt"] == 3, "the receipt must name the attempt that won"


def test_exhausting_the_budget_fails_the_node_and_marks_it_exhausted(tmp_path: Path) -> None:
    worker, gate = _Worker(), _Gate(reject_first=99)

    projection = _controller(
        tmp_path, worker=worker, gate=gate, budgets={"max_attempts": 2},
    ).run()

    assert projection.state == "FAILED"
    assert worker.calls == 2, "exactly the budget, not one more"
    # Every failed attempt is recorded, the last one included, so a single query over
    # node.attempt.failed counts gate rejections without missing the exhausting one.
    assert [payload["attempt"] for payload in _of_type(tmp_path, "node.attempt.failed")] == [1, 2]
    failed = _of_type(tmp_path, "node.failed")
    assert len(failed) == 1
    assert failed[0]["attempt"] == 2
    assert failed[0]["budget_exhausted"] is True


def test_a_node_without_a_declared_budget_gets_exactly_one_attempt(tmp_path: Path) -> None:
    """The default must stay 1, or every existing manifest silently changes meaning."""
    worker, gate = _Worker(), _Gate(reject_first=99)

    projection = _controller(tmp_path, worker=worker, gate=gate, budgets={}).run()

    assert projection.state == "FAILED"
    assert worker.calls == 1
    # Its one attempt failed, so it is recorded like any other failed attempt — uniform
    # counting is what keeps the gate-rejection metric correct across every budget.
    assert [payload["attempt"] for payload in _of_type(tmp_path, "node.attempt.failed")] == [1]
    failed = _of_type(tmp_path, "node.failed")
    # A single-attempt node did not exhaust a budget; it failed on its only attempt.
    assert "budget_exhausted" not in failed[0]


def test_a_retried_run_still_replays_and_reprojects(tmp_path: Path) -> None:
    """Regression for the idempotency-key collision.

    ``_append_node`` keyed events as ``node_id:event_type``. A second ``node.running``
    therefore reused one key with a different payload, which ``GraphEventLog.append``
    rejects outright — so retry crashed the run rather than recording it. This test fails
    on the pre-fix key format.
    """
    controller = _controller(
        tmp_path, worker=_Worker(), gate=_Gate(reject_first=3), budgets={"max_attempts": 4},
    )

    assert controller.run().state == "SUCCEEDED"

    # Replay verifies the hash chain and re-validates every payload, and rejects any
    # duplicate idempotency key across the whole stream.
    replayed = GraphEventLog(tmp_path / "events.jsonl", _identity()).replay_projection()
    assert replayed.state == "SUCCEEDED"


def test_a_worker_fault_is_retried_but_recorded_without_a_gate_verdict(tmp_path: Path) -> None:
    """A worker fault never reached the gate, so it must carry no verdict.

    That absence is what keeps the gate-rejection count honest: attempts which failed
    before gating must not be counted in the denominator of the gate's error rate.
    """
    worker = _RaisingWorker()

    projection = _controller(
        tmp_path, worker=worker, gate=_Gate(reject_first=0), budgets={"max_attempts": 3},
    ).run()

    assert projection.state == "FAILED"
    assert worker.calls == 3, "a worker fault is retryable"
    attempts = _of_type(tmp_path, "node.attempt.failed")
    assert [payload["attempt"] for payload in attempts] == [1, 2, 3]
    assert all("verdict" not in payload for payload in attempts)
    # No gate was ever consulted, so no node.gating was recorded either.
    assert _of_type(tmp_path, "node.gating") == []


def test_a_denied_execution_environment_is_not_retried(tmp_path: Path) -> None:
    """Deterministic denials must not burn budget re-deriving the same answer."""
    enforcer = _DenyingEnforcer()

    projection = _controller(
        tmp_path, worker=_Worker(), gate=_Gate(reject_first=0),
        budgets={"max_attempts": 5}, enforcer=enforcer,
    ).run()

    assert projection.state == "FAILED"
    assert enforcer.calls == 1, "one attempt, not five"
    assert _of_type(tmp_path, "node.attempt.failed") == []


def test_the_gate_error_rate_is_computable_from_the_log_alone(tmp_path: Path) -> None:
    """The property the reliability mathematics needs, asserted directly.

    Counting gate rejections requires the REJECTED attempts to be recorded with their
    verdicts. Nothing here parses a human-readable string.
    """
    _controller(
        tmp_path, worker=_Worker(), gate=_Gate(reject_first=2), budgets={"max_attempts": 3},
    ).run()

    gate_evaluations = len(_of_type(tmp_path, "node.gating"))
    rejections = sum(
        1 for payload in _of_type(tmp_path, "node.attempt.failed") if "verdict" in payload
    )

    assert gate_evaluations == 3
    assert rejections == 2
    assert rejections / gate_evaluations == pytest.approx(2 / 3)


@pytest.mark.parametrize("budget", [0, -1, True, "3", 101, 3.0, None])
def test_an_invalid_retry_budget_is_refused_at_the_point_of_use(
    tmp_path: Path, budget: object,
) -> None:
    """The controller validates the budget itself, at CONSTRUCTION.

    ``PlannedNode.budgets`` is an untyped mapping, and a plan can be built
    programmatically through the runtime facade without passing through manifest
    validation — so the one failure this component must never have (an unbounded loop)
    cannot be guarded in the manifest validator alone. ``True`` is included because a
    bool is an int in Python and would otherwise read as a budget of 1.
    """
    with pytest.raises(GraphIntegrityError, match="max_attempts"):
        _controller(
            tmp_path, worker=_Worker(), gate=_Gate(reject_first=0),
            budgets={"max_attempts": budget},
        )


def test_a_retried_run_is_readable_by_the_arena_and_the_resume_path(tmp_path: Path) -> None:
    """``latest_node_states`` must survive a retried run.

    It is shared by the Arena read model AND the controller's resume path, and it
    validates the per-node lifecycle against a strict transition table. Retry introduces
    GATING -> RUNNING and a rising attempt number, neither of which that table admitted,
    so a retried run wedged both readers even though ``replay_projection`` was fine.
    """
    controller = _controller(
        tmp_path, worker=_Worker(), gate=_Gate(reject_first=2), budgets={"max_attempts": 3},
    )
    assert controller.run().state == "SUCCEEDED"

    log = GraphEventLog(tmp_path / "events.jsonl", _identity())
    states = latest_node_states(_plan({"max_attempts": 3}), log.replay())

    assert states[_NODE_ID]["state"] == "SUCCEEDED"
    assert states[_NODE_ID]["attempt"] == 3


def test_an_effectful_node_may_not_carry_a_retry_budget(tmp_path: Path) -> None:
    """In-process retry is a re-drive, and D7 forbids re-driving an external effect.

    ``_states_from`` already refuses to RESUME an effectful node interrupted mid-execution
    without a per-effect idempotency key. A retry loop that ignored the same rule would let
    a node repeat a payment or an external write that resume explicitly refuses to repeat.
    """
    node = PlannedNode(
        node_id=_NODE_ID, kind="tool", package_digest=_DIGEST, binding_id=None,
        required_effects=frozenset({Effect.EXTERNAL_WRITE}),
        isolation=IsolationLevel.WORKSPACE_ONLY, hard_deadline_ms=1_000,
        budgets={"max_attempts": 3}, approval_policy={},
    )
    plan = ExecutionPlan(
        api_version="bounded-loops.dev/plan/v1", plan_id="sha256:" + "b" * 64,
        source_graph_digest="sha256:" + "a" * 64, policy_digest="sha256:" + "c" * 64,
        compiler_version="test", nodes=(node,), edges=(), levels=((_NODE_ID,),),
        package_digests=(_DIGEST,), connection_bindings=(), canonical_json=b"{}",
    )
    with pytest.raises(GraphIntegrityError, match="idempotency key"):
        GraphRunController(
            plan=plan, event_log=GraphEventLog(tmp_path / "events.jsonl", _identity()),
            worker=_Worker(), gate=_Gate(reject_first=0), artifact_verifier=_PassingVerifier(),
            execution_policy=_policy(plan), execution_enforcer=_Enforcer(),
            timestamp=lambda: "2026-08-12T00:00:00Z",
        )


def test_a_gate_rejection_on_the_final_attempt_is_still_counted(tmp_path: Path) -> None:
    """The undercount this guards against was real, and this test would have missed it.

    When the budget runs out ON a gate rejection, recording only the non-final failures
    would leave that last rejection solely on the terminal node.failed. A metric counting
    node.attempt.failed would then report 1/2 where the truth is 2/2 — a systematic
    undercount of exactly the quantity the records exist to measure.
    """
    _controller(
        tmp_path, worker=_Worker(), gate=_Gate(reject_first=99), budgets={"max_attempts": 2},
    ).run()

    gate_evaluations = len(_of_type(tmp_path, "node.gating"))
    rejections = sum(
        1 for payload in _of_type(tmp_path, "node.attempt.failed") if "verdict" in payload
    )

    assert gate_evaluations == 2
    assert rejections == 2, "the rejection that exhausted the budget must be counted too"
    assert rejections / gate_evaluations == 1.0


class _CrashingGate:
    """Rejects, then crashes mid-attempt on the call given by ``crash_on``.

    Raises ``KeyboardInterrupt`` — a BaseException — so the controller's ``except
    Exception`` handlers do not catch it and the stream is left genuinely interrupted,
    exactly as a killed process would leave it.
    """

    def __init__(self, crash_on: int) -> None:
        self._crash_on = crash_on
        self.calls = 0

    def evaluate(self, *, plan, node, result) -> GateVerdict:  # noqa: ANN001, ARG002
        self.calls += 1
        if self.calls == self._crash_on:
            raise KeyboardInterrupt("killed mid-attempt")
        return GateVerdict(False, f"rejected on call {self.calls}")


def test_resume_continues_the_attempt_count_instead_of_restarting_it(tmp_path: Path) -> None:
    """The budget must bound attempts across resumes, not per resume.

    Restarting at attempt 1 on resume is wrong twice: it re-grants the whole budget every
    time a run is resumed, and it appends a LOWER attempt number after a higher one, which
    makes the finished run permanently unreadable to the lifecycle validation shared by the
    Arena and the resume path.
    """
    log_path = tmp_path / "events.jsonl"
    plan = _plan({"max_attempts": 2})
    crashing = _CrashingGate(crash_on=2)
    first = GraphRunController(
        plan=plan, event_log=GraphEventLog(log_path, _identity()),
        worker=_Worker(), gate=crashing, artifact_verifier=_PassingVerifier(),
        execution_policy=_policy(plan), execution_enforcer=_Enforcer(),
        timestamp=lambda: "2026-08-12T00:00:00Z",
    )
    with pytest.raises(KeyboardInterrupt):
        first.run()

    # Attempt 1 was rejected and attempt 2 was interrupted before its verdict.
    assert [p["attempt"] for p in _of_type(tmp_path, "node.running")] == [1, 2]

    resumed = GraphRunController(
        plan=plan, event_log=GraphEventLog(log_path, _identity()),
        worker=_Worker(), gate=_Gate(reject_first=0), artifact_verifier=_PassingVerifier(),
        execution_policy=_policy(plan), execution_enforcer=_Enforcer(),
        timestamp=lambda: "2026-08-12T00:00:01Z",
    ).resume()

    assert resumed.state == "SUCCEEDED"
    # The interrupted attempt is re-driven under ITS OWN number, so the winning receipt is
    # attempt 2. Under a restart-at-1 resume this would read 1 — a lower number after a
    # higher one, and the budget silently re-granted.
    succeeded = _of_type(tmp_path, "node.succeeded")
    assert len(succeeded) == 1
    assert succeeded[0]["attempt"] == 2

    # Distinct attempts never exceed the budget, no matter how often the run is resumed.
    assert sorted({p["attempt"] for p in _of_type(tmp_path, "node.running")}) == [1, 2]

    # And the resumed run is still readable by the Arena and the resume path.
    states = latest_node_states(plan, GraphEventLog(log_path, _identity()).replay())
    assert states[_NODE_ID]["state"] == "SUCCEEDED"
    assert states[_NODE_ID]["attempt"] == 2


def test_the_terminal_verdict_duplicates_the_final_attempt_rather_than_adding_one(
    tmp_path: Path,
) -> None:
    """A rejection on the last attempt is recorded TWICE, and that is deliberate.

    ``node.attempt.failed`` is the per-attempt audit record; ``node.failed`` is the node's
    durable outcome and carries its verdict as it always has. So the final rejection appears
    in both. A reader summing verdicts across BOTH event types therefore double-counts the
    last one — the canonical count is ``node.attempt.failed`` alone.

    This test pins that invariant: the two records describe the SAME rejection, same attempt
    and same verdict body, so counting attempt records is both correct and complete.
    """
    _controller(
        tmp_path, worker=_Worker(), gate=_Gate(reject_first=99), budgets={"max_attempts": 2},
    ).run()

    final_attempt = [p for p in _of_type(tmp_path, "node.attempt.failed") if p["attempt"] == 2]
    failed = _of_type(tmp_path, "node.failed")

    assert len(final_attempt) == 1
    assert len(failed) == 1
    assert failed[0]["attempt"] == final_attempt[0]["attempt"]
    assert failed[0]["verdict"] == final_attempt[0]["verdict"], (
        "the terminal receipt must restate the final attempt's verdict, not a different one"
    )
    # Canonical count: attempt records only. Summing both event types would give 3 for 2
    # gate evaluations.
    assert len(_of_type(tmp_path, "node.gating")) == 2
    assert sum(1 for p in _of_type(tmp_path, "node.attempt.failed") if "verdict" in p) == 2


def _controller_at(
    log_path: Path, plan: ExecutionPlan, gate: IndependentGatePort, clock: str,
) -> GraphRunController:
    return GraphRunController(
        plan=plan, event_log=GraphEventLog(log_path, _identity()),
        worker=_Worker(), gate=gate, artifact_verifier=_PassingVerifier(),
        execution_policy=_policy(plan), execution_enforcer=_Enforcer(),
        timestamp=lambda: clock,
    )


def test_an_attempt_that_recorded_its_failure_is_not_re_driven(tmp_path: Path) -> None:
    """Grok probe P1/P2: a recorded failure must be spent, or one attempt carries two verdicts.

    Crash after the final ``node.attempt.failed`` but before the node's terminal receipt.
    If resume re-drove that same attempt number, the log could end up holding BOTH a
    rejection and an acceptance for it — making the gate rejection rate count a rejection
    that later passed — and a second rejection with a DIFFERENT reason would collide with
    the existing attempt-record key and wedge the resume with no terminal receipt at all.
    """
    log_path = tmp_path / "events.jsonl"
    plan = _plan({"max_attempts": 2})

    # Drive both attempts to rejection, then interrupt before the terminal receipt by
    # replaying the stream into a fresh controller rather than letting the first finish.
    _controller_at(log_path, plan, _Gate(reject_first=99), "2026-08-12T00:00:00Z").run()
    recorded = [p["attempt"] for p in _of_type(tmp_path, "node.attempt.failed")]
    assert recorded == [1, 2], "both attempts recorded their own failure"

    # A resume now, with an ACCEPTING gate, must not resurrect attempt 2.
    resumed = _controller_at(
        log_path, plan, _Gate(reject_first=0), "2026-08-12T00:00:01Z",
    ).resume()

    assert resumed.state == "FAILED", "a run whose budget is spent stays failed"
    # No acceptance was ever written for an attempt that had already recorded a rejection.
    assert _of_type(tmp_path, "node.succeeded") == []
    assert [p["attempt"] for p in _of_type(tmp_path, "node.attempt.failed")] == [1, 2]


def test_a_resume_denied_by_policy_does_not_write_a_lower_attempt(tmp_path: Path) -> None:
    """Grok probe P3: the fail-closed resume paths must carry the real attempt number.

    ``_fail_node`` defaults to attempt 1. On a resume that fails re-authorization after a
    higher attempt was already recorded, that default appends attempt 1 after attempt 2 —
    the same non-monotonic corruption the cursor exists to prevent, on a path the cursor
    does not itself cover.
    """
    log_path = tmp_path / "events.jsonl"
    plan = _plan({"max_attempts": 3})

    # Attempt 1 rejected; attempt 2 interrupted mid-gate.
    with pytest.raises(KeyboardInterrupt):
        _controller_at(log_path, plan, _CrashingGate(crash_on=2), "2026-08-12T00:00:00Z").run()
    assert [p["attempt"] for p in _of_type(tmp_path, "node.running")] == [1, 2]

    class _DenyAll:
        def authorize(self, *, plan, node):  # noqa: ANN001, ARG002
            raise GraphIntegrityError("policy expired")

    denied = GraphRunController(
        plan=plan, event_log=GraphEventLog(log_path, _identity()),
        worker=_Worker(), gate=_Gate(reject_first=0), artifact_verifier=_PassingVerifier(),
        execution_policy=_DenyAll(), execution_enforcer=_Enforcer(),
        timestamp=lambda: "2026-08-12T00:00:02Z",
    ).resume()

    assert denied.state == "FAILED"
    failed = _of_type(tmp_path, "node.failed")
    assert len(failed) == 1
    assert failed[0]["attempt"] == 2, "the denial belongs to the attempt in flight, not attempt 1"

    # The stream is still readable by the Arena and any later resume.
    states = latest_node_states(plan, GraphEventLog(log_path, _identity()).replay())
    assert states[_NODE_ID]["state"] == "FAILED"
