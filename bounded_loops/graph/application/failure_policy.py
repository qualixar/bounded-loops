"""Which failures a run may continue past, and what states let a run end SUCCEEDED.

Extracted from the controller so the rule can be read — and audited — on its own. Getting this set
wrong is a security regression rather than a bug: continuing past the wrong failure means a run keeps
spending money, trusts a gate that has already proved unreliable, or routes around a control that
said no.
"""

from __future__ import annotations

from bounded_loops.graph.application.schedule_ready import NodeState
from bounded_loops.graph.domain.events import NodeFailureCause

#: The node states a SUCCEEDED run may end in. SKIPPED belongs here — a conditional graph whose
#: untaken branch was correctly skipped did not fail. FAILED deliberately does NOT, even when a
#: ``failed``-conditioned recovery edge ran afterwards: whether a handled failure clears the run is a
#: repair-semantics question and belongs with repair edges. Until then, a failure is a failure.
RUN_SUCCEEDS_ON = frozenset({NodeState.SUCCEEDED, NodeState.SKIPPED})

#: The ONLY failure causes a run may continue past. Every one is the node's own bounded-loop
#: outcome: it attempted, and its budget ran out.
MAY_CONTINUE_AFTER = frozenset({
    #: The independent gate read the output and refused it, on every attempt. The canonical case,
    #: and the one a failure-conditioned edge exists to route around.
    NodeFailureCause.GATE_REJECTED,
    #: The worker raised before producing a result. The node's own execution failed.
    NodeFailureCause.WORKER_FAULT,
    #: The worker returned, but its declared artifacts did not verify. Still this node's output.
    NodeFailureCause.ARTIFACT_UNVERIFIED,
    #: A resume found this node's retry budget already spent — no attempts left.
    NodeFailureCause.BUDGET_SPENT,
    #: One attempt was re-driven too many times without completing.
    NodeFailureCause.REDRIVE_EXHAUSTED,
})

#: Everything else stops the run whatever the fail mode, and each for a stated reason:
#:
#: * ``GATE_BROKEN`` — the gate itself failed or returned nonsense. No later verdict from it can be
#:   trusted, so continuing would keep gating on a known-unreliable authority.
#: * ``POLICY_DENIED`` / ``ENVIRONMENT_DENIED`` — a control refused. Routing around a refusal
#:   defeats the control.
#: * ``APPROVAL_REJECTED`` — a human said no. Letting the graph find another path past a human
#:   rejection would defeat the approval gate.
#: * ``APPROVAL_UNRESOLVED`` — the approval machinery is broken or missing.
#: * ``SPEND_EXHAUSTED`` — the money cap is reached. Continuing spends more of it.
#: * ``NO_WORKER`` — deployment misconfiguration; it will recur on every similar node.
#: * ``WORKER_CONTRACT`` — the worker broke its contract. A defect, likely shared by other nodes.
#: * ``BUDGET_UNMEASURABLE`` — an authoring error: a declared budget no worker can report.


def may_continue(cause: NodeFailureCause, *, continue_on_failure: bool) -> bool:
    """Whether the run keeps driving the graph after a node failed with ``cause``.

    ``continue_on_failure`` is the graph's fail mode reduced to one bit. Under the default
    (``fail_mode: fail_closed``) this is always ``False``, so behaviour is unchanged.
    """
    return continue_on_failure and cause in MAY_CONTINUE_AFTER


#: The authoring fail mode that stops a run at the first node failure. Anything else keeps driving
#: the graph, which is what a failure-conditioned edge needs to be admitted at all.
HALT_AT_FIRST_FAILURE = "fail_closed"


def continues_after_failure(fail_mode: str | None) -> bool:
    """Reduce a graph's ``fail_mode`` to the one bit the controller acts on.

    ``None`` or an unknown value reduces to False — halt. A run directory written before the fail
    mode was recorded has no value to read, and defaulting to the stricter behaviour keeps such a
    run replaying exactly as it originally ran. A plan that actually NEEDS continuation is not
    silently downgraded: the controller refuses to be built at all in that case.
    """
    return fail_mode is not None and fail_mode != HALT_AT_FIRST_FAILURE


def recorded_fail_mode(meta: dict[str, object]) -> str | None:
    """The fail mode a run was STARTED under, as recorded in its ``run-meta.json``.

    ``None`` when absent — a run directory written before the mode was recorded. Paired with
    ``continues_after_failure`` that reduces to halt-at-first-failure, which is exactly how such a
    run originally executed, so a resume cannot change a completed run's semantics.
    """
    value = meta.get("fail_mode")
    return value if isinstance(value, str) and value else None
