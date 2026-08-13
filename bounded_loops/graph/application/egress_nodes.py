"""Is a node's work an authorized network call, or a sandboxed subprocess?

One predicate, pulled out of the controller for the 800-line cap. It is a property of the plan
and the deployment's declared egress transports, not of the run, so it belongs beside the plan
rather than inside the state machine.
"""

from __future__ import annotations

from bounded_loops.graph.domain.plan import ExecutionPlan, PlannedNode


def is_egress_node(plan: ExecutionPlan, node: PlannedNode, egress_transports: frozenset[str]) -> bool:
    """A connector/EGRESS node's work is an authorized network call over an admitted connection
    (a frontier model API), NOT a sandboxed subprocess — so it is routed to the connector worker
    and does NOT pass the process-isolation enforcer (egress is authorized inside the connector
    path). It is identified by being bound to a connection whose transport the deployment has
    declared an egress transport; ``egress_transports`` defaults to empty, so nothing is egress
    unless a deployment opts in (e.g. a local_cli connector stays a sandboxed subprocess)."""
    if node.binding_id is None:
        return False
    transport = next(
        (binding.transport for binding in plan.connection_bindings if binding.binding_id == node.binding_id),
        None,
    )
    return transport is not None and transport in egress_transports
