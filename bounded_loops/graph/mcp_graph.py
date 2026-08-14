"""Expose the graph runtime over MCP — a thin transport shim (task #4).

Like ``bounded_loops.mcp_server`` for the loop engine, no business logic lives here: each tool
validates its MCP inputs, enforces tenant scoping, and translates a call into the ALREADY-audited
graph use cases behind an injected ``GraphRuntimeFacade``, then shapes the result (or a
GraphError) into a tool dict. The MCP module never wires real workers / isolation / connectors /
approval authority — the DEPLOYMENT provides the facade, so this stays engine-agnostic and
unit-testable with a fake facade (and imports no `mcp` dependency in its logic; `register` only
uses an MCPServer instance handed in).

TRUST MODEL: an MCP caller supplies INTENT (which run / node); the deployment injects the
AUTHENTICATED subject (via ``register``'s ``subject_provider``, read from the MCP session — never an
LLM argument), and the facade binds AUTHORITY (derives the approval/actor context from that subject
and runs the real authorized use case). No subject spoof, signature, or credential ever crosses the
MCP boundary; cross-tenant access is denied by the facade's authorizer. Read tools are
side-effect-free; every MUTATING tool (resume, approve) is gated by a real server-side ``confirm``:
``confirm=False`` returns a preview and changes nothing, ``confirm=True`` applies. This is a plain
per-call gate — a weaker affordance than ``bl_run``'s preview-then-confirm handshake, which is
proportionate to run-control's bounded blast radius (resume re-drives a paused run; approve records
a decision; both fail closed on invalid state). A deployment needing the stricter handshake layers
it in the facade.
"""

from __future__ import annotations

from typing import Callable, Protocol

from bounded_loops.graph.application.node_spend import RunBudget
from bounded_loops.graph.application.arena_projection import ArenaProjection, ArenaReadRequest
from bounded_loops.graph.application.state_document import render_state_markdown
from bounded_loops.graph.domain.errors import GraphError, GraphValidationError

_DECISIONS = frozenset({"approved", "rejected"})


class GraphRuntimeFacade(Protocol):
    """Deployment-owned: locates a run, wires the real controller ports + authorizer, and runs the
    authorized use case. Every method authorizes the (subject, org, project, run) itself; the MCP
    shim never sees a port or credential. ``status`` is read-only; ``resume``/``approve`` mutate."""

    def status(self, request: ArenaReadRequest) -> ArenaProjection: ...

    def resume(
        self, request: ArenaReadRequest, *, run_budget: RunBudget | None = None,
    ) -> ArenaProjection: ...

    def approve(
        self, request: ArenaReadRequest, *, node_id: str, decision: str,
        run_budget: RunBudget | None = None,
    ) -> ArenaProjection: ...


def graph_status(facade: GraphRuntimeFacade, payload: dict) -> dict:
    """Read a run's Arena projection (side-effect-free)."""
    return _guarded(lambda: {"ok": True, "projection": _projection_dict(facade.status(_read_request(payload)))})


def graph_state_md(facade: GraphRuntimeFacade, payload: dict) -> dict:
    """Render a run's STATE.md — the human-readable projection (side-effect-free)."""
    return _guarded(lambda: {"ok": True, "state_md": render_state_markdown(facade.status(_read_request(payload)))})


def _ceiling(max_tokens: int | None, max_cost_usd: str | None) -> RunBudget | None:
    """A spend ceiling for one continuation, or ``None`` to use the facade's own.

    A run that paused on its ceiling cannot be continued without one — the controller refuses,
    because continuing with no limit is the opposite of what a budget pause asks for. Exposing
    it here is what makes a paused run resumable over MCP at all.
    """
    from bounded_loops.graph.application.budget_config import usd_to_microunits

    if max_tokens is None and max_cost_usd is None:
        return None
    return RunBudget(
        max_tokens=max_tokens,
        max_cost_microunits=usd_to_microunits(max_cost_usd) if max_cost_usd else None,
    )


def graph_resume(
    facade: GraphRuntimeFacade, payload: dict, *, confirm: bool = False,
    max_tokens: int | None = None, max_cost_usd: str | None = None,
) -> dict:
    """Resume an interrupted run. MUTATING: gated by a server-side confirm."""
    def _run() -> dict:
        request = _read_request(payload)
        if not confirm:
            return _preview(f"resume run {request.run_id!r}")
        return {"ok": True, "projection": _projection_dict(
            facade.resume(request, run_budget=_ceiling(max_tokens, max_cost_usd)),
        )}

    return _guarded(_run)


def graph_approve(
    facade: GraphRuntimeFacade, payload: dict, *, node_id: str, decision: str,
    confirm: bool = False, max_tokens: int | None = None, max_cost_usd: str | None = None,
) -> dict:
    """Record a human decision for an approval node so a paused run can continue. MUTATING: gated
    by a server-side confirm. The caller supplies only the decision + node; the facade binds the
    authority from the authenticated subject."""
    def _run() -> dict:
        request = _read_request(payload)
        _validate_node_id(node_id)
        _validate_decision(decision)
        if not confirm:
            return _preview(f"{decision} approval node {node_id!r} in run {request.run_id!r}")
        projection = facade.approve(
            request, node_id=node_id, decision=decision,
            run_budget=_ceiling(max_tokens, max_cost_usd),
        )
        return {"ok": True, "projection": _projection_dict(projection)}

    return _guarded(_run)


def register(mcp: object, facade: GraphRuntimeFacade, *, subject_provider: Callable[[], str]) -> None:
    """Wire the graph tools onto an MCPServer instance handed in by the deployment. Thin glue — the
    only SDK-touching code; the tool logic stays in the pure handlers above.

    SECURITY: ``subject_id`` is NOT an LLM tool parameter — it is derived per call from
    ``subject_provider`` (the deployment reads the AUTHENTICATED subject from the MCP session,
    not the model's arguments). The LLM controls only INTENT (which org/project/run/node); it can
    never claim to be another subject, so an approval/resume is always attributed to the real
    authenticated actor. Cross-tenant is additionally denied by the facade's authorizer."""
    tool = mcp.tool  # type: ignore[attr-defined]

    @tool()
    def graph_status_tool(organization_id: str, project_id: str, run_id: str) -> dict:
        return graph_status(facade, _payload(subject_provider(), organization_id, project_id, run_id))

    @tool()
    def graph_state_md_tool(organization_id: str, project_id: str, run_id: str) -> dict:
        return graph_state_md(facade, _payload(subject_provider(), organization_id, project_id, run_id))

    @tool()
    def graph_resume_tool(
        organization_id: str, project_id: str, run_id: str, confirm: bool = False,
        max_tokens: int | None = None, max_cost_usd: str | None = None,
    ) -> dict:
        """Resume a run. A run that paused on its spend ceiling needs a new one supplied here —
        continuing with no limit is not what a budget pause is asking for."""
        return graph_resume(
            facade, _payload(subject_provider(), organization_id, project_id, run_id),
            confirm=confirm, max_tokens=max_tokens, max_cost_usd=max_cost_usd,
        )

    @tool()
    def graph_approve_tool(
        organization_id: str, project_id: str, run_id: str,
        node_id: str, decision: str, confirm: bool = False,
    ) -> dict:
        return graph_approve(
            facade, _payload(subject_provider(), organization_id, project_id, run_id),
            node_id=node_id, decision=decision, confirm=confirm,
        )


def _payload(subject_id: str, organization_id: str, project_id: str, run_id: str) -> dict:
    return {"subject_id": subject_id, "organization_id": organization_id, "project_id": project_id, "run_id": run_id}


def _read_request(payload: dict) -> ArenaReadRequest:
    if not isinstance(payload, dict):
        raise GraphValidationError("mcp_request", "/", "request must be an object")
    fields = {}
    for name in ("subject_id", "organization_id", "project_id", "run_id"):
        value = payload.get(name)
        if not isinstance(value, str) or not value.strip():
            raise GraphValidationError("mcp_request", f"/{name}", f"{name} must be a non-empty string")
        fields[name] = value
    return ArenaReadRequest(**fields)


def _validate_node_id(node_id: str) -> None:
    if not isinstance(node_id, str) or not node_id.strip():
        raise GraphValidationError("mcp_approve", "/node_id", "node_id must be a non-empty string")


def _validate_decision(decision: str) -> None:
    if decision not in _DECISIONS:
        raise GraphValidationError("mcp_approve", "/decision", "decision must be 'approved' or 'rejected'")


def _preview(what: str) -> dict:
    return {"ok": True, "preview": True, "would": what, "hint": "re-call with confirm=true to apply"}


def _guarded(run) -> dict:
    # Translate a fail-closed GraphError into a tool-shaped error dict (never leak a stack trace);
    # any other exception propagates (a genuine bug, not an expected engine outcome).
    try:
        return run()
    except GraphError as exc:
        # GraphValidationError carries code/pointer/message; GraphIntegrityError carries only a
        # message — shape both without leaking a stack trace.
        return {"ok": False, "error": {
            "code": getattr(exc, "code", "graph_integrity"),
            "pointer": getattr(exc, "pointer", "/"),
            "message": getattr(exc, "message", str(exc)),
        }}


def _projection_dict(projection: ArenaProjection) -> dict:
    return {
        "organization_id": projection.organization_id,
        "project_id": projection.project_id,
        "run_id": projection.run_id,
        "run_state": projection.run_state,
        "receipt_sequence": projection.receipt_sequence,
        "receipt_head_hash": projection.receipt_head_hash,
        "graph_digest": projection.graph_digest,
        "plan_digest": projection.plan_digest,
        "policy_digest": projection.policy_digest,
        "nodes": [
            {
                "node_id": node.node_id, "kind": node.kind, "state": node.state, "attempt": node.attempt,
                "required_effects": list(node.required_effects), "isolation": node.isolation,
                "transport": node.transport, "artifact_digests": list(node.artifact_digests),
                # The gate's own verdict and words. Every other field says what ran; this says
                # why it counted as verified, so it is the field a reader should look at first.
                "gate_passed": node.gate_passed, "gate_reason": node.gate_reason,
            }
            for node in projection.nodes
        ],
        "edges": [list(edge) for edge in projection.edges],
        "levels": [list(level) for level in projection.levels],
    }
