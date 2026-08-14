"""Reference composition for the bounded-loops graph engine.

This module is a deployment-wiring guide, not a test.  It shows:

A) ``execute_graph_run`` — compile and execute a Local-CLI graph from a manifest
   plus an admitted-connection record.  A commented block shows the BYOK/https
   variant; supply your own ``AdmittedConnectionRecord`` map for that path.

B) ``LocalGraphRuntimeFacade`` — the concrete, file-backed runtime facade.
   Shows how to construct it with ``SameTenantArenaAuthorizer`` and call
   ``status``, ``resume``, and ``approve`` over a persisted run directory.

C) ``mcp_graph.register`` — a commented snippet that wires the facade onto an
   ``MCPServer`` instance.  The ``mcp`` package is optional
   (``pip install bounded-loops[mcp]``, which resolves ``mcp>=2,<3``); it is NOT
   imported at module load time so the module stays importable without it.

All runnable code lives under ``if __name__ == "__main__":``; the module is
import-clean and side-effect-free above that guard.

Deployment seams (what a client supplies):
  * Agent CLI binaries (``claude``, ``codex``, ``grok``, ``muse``, ``agy``)
    already logged in on the host.
  * ``AdmittedConnectionRecord`` map (BYOK/https path only): carries endpoint
    host and credential ENV-VAR name — never the credential value.
  * A real ``subject_provider`` for MCP: reads the authenticated subject from
    the MCP session; must never accept a subject from LLM tool arguments.
  * For a hosted deployment: a role-checking ``ApprovalAuthorizationPort`` and
    a crypto ``ApprovalSignatureVerifierPort`` instead of the local defaults.

See ``docs/graph-reference-composition.md`` for the narrative explanation.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Mapping

from bounded_loops.graph.application.arena_projection import (
    ArenaProjection,
    ArenaReadRequest,
)
from bounded_loops.graph.graph_composition import execute_graph_run
from bounded_loops.graph.graph_runtime_facade import (
    LocalGraphRuntimeFacade,
    SameTenantArenaAuthorizer,
)

# ─────────────────────────────────────────────────────────────────────────────
# SECTION A — execute_graph_run: compile and run a graph
# ─────────────────────────────────────────────────────────────────────────────

# Inline manifest (matches the graph-quickstart example).  A real deployment
# reads this from disk; inlined here so the module has no path dependency.
_EXAMPLE_MANIFEST_YAML: str = """\
api_version: "bounded-loops.dev/graph/v1"
graph_id: agent-run
version: "1.0.0"
nodes:
  - id: agent
    kind: research_claim
    inputs: {}
    outputs: {claim: text}
    budget: {max_attempts: 1, max_wallclock_s: 30}
    effects: [workspace_write]
    isolation: process_restricted
    connection_slot: model
edges: []
connection_slots:
  - id: model
    requires: [text_generation]
    data_class_max: public
policies:
  data_class: public
  fail_mode: fail_closed
"""

# Admitted-connection record for a Local-CLI node.  Use real sha256 digests in
# production; the placeholder hashes below are accepted by the compiler for
# local development.
_EXAMPLE_CONNECTIONS: list[object] = [
    {
        "binding_id": "binding-1",
        "slot_id": "model",
        "connector_id": "local-cli",
        "connector_version": "1.0.0",
        "connection_id": "conn-1",
        "admission_digest": "sha256:" + "b" * 64,
        "route_policy_digest": "sha256:" + "c" * 64,
        "provider_id": "claude",
        "model_target": "subscription",
        "region": "local",
        "fallback": False,
        "capabilities": ["text_generation"],
        "data_class_max": "public",
        "allowed_effects": ["workspace_write"],
        "isolation": "process_restricted",
        "transport": "local_cli",
        "admitted": True,
    }
]


def run_local_cli_graph(
    out_dir: Path,
    *,
    manifest_text: str = _EXAMPLE_MANIFEST_YAML,
    connections_raw: list[object] | None = None,
    node_prompts: Mapping[str, str] | None = None,
    json_out: bool = False,
) -> int:
    """Compile and run an admitted Local-CLI graph end-to-end.

    The engine compiles the manifest, probes platform sandbox support, invokes
    the user's own subscription-mode agent CLI (``claude``, ``codex``, etc.) as
    a subprocess, gates on the independent structural gate, and persists the run
    directory.

    The run-time prompt is NOT persisted in the run directory.  Re-supply it on
    every ``resume`` call.

    Returns:
        0  — all nodes SUCCEEDED.
        2  — compile error, preflight refusal, or a node failure.
    """
    effective_conn: list[object] = (
        connections_raw if connections_raw is not None else _EXAMPLE_CONNECTIONS
    )
    effective_prompts: Mapping[str, str] = (
        node_prompts if node_prompts is not None else {}
    )
    return execute_graph_run(
        manifest_text=manifest_text,
        manifest_suffix=".yaml",
        connections_raw=effective_conn,
        node_prompts=effective_prompts,
        out_dir=out_dir,
        json_out=json_out,
    )


# ── BYOK/https variant (commented — requires AdmittedConnectionRecord) ────────
#
# For graphs whose nodes use an https-transport connector, supply a map of
# connection_id → AdmittedConnectionRecord and pass it as admitted_connections.
# The record carries the endpoint + credential ENV-VAR name (never the value).
# Credentials are resolved from the environment at run time.
#
#     from bounded_loops.graph.adapters.connectors.admitted_connection_request import (
#         AdmittedConnectionRecord,
#     )
#
#     admitted = {
#         "conn-https-1": AdmittedConnectionRecord.from_mapping({
#             "connection_id": "conn-https-1",
#             "endpoint_host": "api.openai.com",
#             "credential_env_var_name": "OPENAI_API_KEY",
#             "credential_header_name": "Authorization",
#             "credential_header_prefix": "Bearer ",
#             ...  # see AdmittedConnectionRecord.from_mapping for all fields
#         })
#     }
#
#     exit_code = execute_graph_run(
#         manifest_text=manifest_text,
#         manifest_suffix=".yaml",
#         connections_raw=connections_raw,
#         node_prompts=node_prompts,
#         out_dir=out_dir,
#         admitted_connections=admitted,
#     )
#
# A graph may mix local_cli and https nodes; the engine routes each node to the
# right sub-worker by its binding transport.  Preflight fails closed if an
# https node has no matching admitted record.
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# SECTION B — LocalGraphRuntimeFacade: status / resume / approve
# ─────────────────────────────────────────────────────────────────────────────

def build_local_facade(
    runs_root: Path,
    *,
    node_prompts: Mapping[str, str] | None = None,
) -> LocalGraphRuntimeFacade:
    """Construct a ``LocalGraphRuntimeFacade`` for local same-tenant runs.

    ``SameTenantArenaAuthorizer`` allows reads and mutations only when the
    subject IS the organization.  For a hosted deployment, supply a
    role-checking ``ArenaAuthorizationPort``, an ``ApprovalAuthorizationPort``
    that verifies the actor's role against your identity provider, and a crypto
    ``ApprovalSignatureVerifierPort``.

    ``node_prompts`` is NOT persisted in the run directory.  Re-supply prompts
    for every non-terminal connector node on each ``resume`` / ``approve`` call.

    Run directory layout expected:
        runs_root / <org> / <project> / <run_id> /
            plan.json, manifest.yaml, connections.json, run-meta.json,
            controller-events.jsonl, [approvals.json], artifacts/
    """
    effective_prompts: Mapping[str, str] = (
        node_prompts if node_prompts is not None else {}
    )
    return LocalGraphRuntimeFacade(
        runs_root=runs_root,
        arena_authorizer=SameTenantArenaAuthorizer(),
        node_prompts=effective_prompts,
        # admitted_connections=None  ← local-CLI-only default.  Pass a
        #   Mapping[str, AdmittedConnectionRecord] for graphs with https nodes.
        # approval_authorizer=None   ← local default: SameTenantApprovalAuthorizer.
        #   A hosted deployment injects a role-checking ApprovalAuthorizationPort.
        # approval_signature_verifier=None  ← local default: accepts any non-empty
        #   signature (MCP session is the auth boundary for local runs).  A hosted
        #   deployment injects a real crypto ApprovalSignatureVerifierPort.
    )


def facade_status(
    facade: LocalGraphRuntimeFacade,
    *,
    subject_id: str,
    organization_id: str,
    project_id: str,
    run_id: str,
) -> ArenaProjection:
    """Read the current Arena projection for a persisted run (side-effect-free)."""
    request = ArenaReadRequest(
        subject_id=subject_id,
        organization_id=organization_id,
        project_id=project_id,
        run_id=run_id,
    )
    return facade.status(request)


def facade_resume(
    facade: LocalGraphRuntimeFacade,
    *,
    subject_id: str,
    organization_id: str,
    project_id: str,
    run_id: str,
    node_prompts: Mapping[str, str],
) -> ArenaProjection:
    """Resume an interrupted run, returning the post-resume projection.

    ``node_prompts`` must cover every non-terminal connector node in the run.
    The facade fails closed with a clear message if any connector node is
    non-terminal and its node_id is absent from the supplied prompts.

    Resuming a run that is already terminal is idempotent.

    Uses ``dataclasses.replace`` to create a scope-local view of the facade
    with the supplied prompts, rather than mutating the shared instance.
    """
    scoped = dataclasses.replace(facade, node_prompts=node_prompts)
    request = ArenaReadRequest(
        subject_id=subject_id,
        organization_id=organization_id,
        project_id=project_id,
        run_id=run_id,
    )
    return scoped.resume(request)


def facade_approve(
    facade: LocalGraphRuntimeFacade,
    *,
    subject_id: str,
    organization_id: str,
    project_id: str,
    run_id: str,
    node_id: str,
    decision: str,
    node_prompts: Mapping[str, str] | None = None,
) -> ArenaProjection:
    """Record a human decision for an approval node and resume the run.

    ``decision`` must be ``"approved"`` or ``"rejected"``.

    For ``"approved"``: runs the full ``approvals.approve`` use case — validates
    authority and signature, then persists the decision durably to
    ``runs_root/<org>/<project>/<run_id>/approvals.json`` before the run
    continues.  Fail-closed at every step.

    For ``"rejected"``: records an in-memory rejection and fails the run closed.
    The rejection is not durably persisted (a known follow-up: durable rejection).

    Local security posture: the local defaults use same-tenant authorization and
    accept any non-empty signature (MCP session IS the auth boundary for local
    runs).  A hosted deployment injects a real ``ApprovalAuthorizationPort`` and
    ``ApprovalSignatureVerifierPort`` into the facade.
    """
    effective_prompts: Mapping[str, str] = (
        node_prompts if node_prompts is not None else {}
    )
    scoped = dataclasses.replace(facade, node_prompts=effective_prompts)
    request = ArenaReadRequest(
        subject_id=subject_id,
        organization_id=organization_id,
        project_id=project_id,
        run_id=run_id,
    )
    return scoped.approve(request, node_id=node_id, decision=decision)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION C — MCP surface (commented; requires bounded-loops[mcp])
# ─────────────────────────────────────────────────────────────────────────────
#
# Wire the facade onto an MCPServer instance in your deployment entrypoint.
# Do NOT do this at module import time.
#
#     from mcp.server.mcpserver import MCPServer
#     from bounded_loops.graph.mcp_graph import register
#
#     def make_mcp_server(runs_root: Path) -> MCPServer:
#         mcp = MCPServer("bounded-loops-graph")
#         facade = build_local_facade(runs_root)
#
#         # SECURITY: subject_provider is called once per tool invocation and
#         # reads the authenticated subject from the MCP session.  It is never
#         # an LLM tool argument — the model controls only intent (which
#         # org/project/run/node); it cannot spoof the authenticated subject.
#         #
#         # There is NO SDK call that returns this for you. MCP 2.0 removed
#         # `Context.client_id`, and an MCP session is not an authenticated
#         # identity in the first place — a stdio server's peer is whoever
#         # spawned the process. Wire this to your own authentication (the OS
#         # user for a local server; the verified token subject for a hosted
#         # one), and read `mcp.server.auth` / `token_verifier` if the server
#         # is reachable over HTTP.
#         def get_subject() -> str:
#             raise NotImplementedError("bind to your authenticated identity")
#
#         register(mcp, facade, subject_provider=get_subject)
#         return mcp
#
# Exposed MCP tools (subject always from subject_provider; never from the model):
#   graph_status_tool    — side-effect-free Arena projection
#   graph_state_md_tool  — Markdown-rendered projection
#   graph_resume_tool    — confirm=False → preview; confirm=True → applies
#   graph_approve_tool   — confirm=False → preview; confirm=True → applies
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# Runnable demo (no side effects above this line)
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # All execution examples are commented out.  Uncomment the block that
    # matches your setup.

    # ── A: run a Local-CLI graph ─────────────────────────────────────────────
    # Requires: a supported agent CLI (e.g. `claude`) installed and logged in.
    #
    # import tempfile
    # _out = Path(tempfile.mkdtemp(prefix="bl-ref-"))
    # exit_code = run_local_cli_graph(
    #     _out / "my-run",
    #     node_prompts={"agent": "Summarise AI reliability engineering, 100 words."},
    # )
    # print(f"execute_graph_run exit code: {exit_code}")
    # print(f"Inspect run:  bl graph status --run {_out / 'my-run'}")

    # ── B: status / resume / approve over an existing run ────────────────────
    # Run `bl graph demo --out <dir>` first to produce a run directory, then
    # point runs_root at its parent and supply the right org/project/run_id.
    #
    # _runs = Path("/tmp/bl-runs")          # parent of <org>/<project>/<run_id>
    # _org, _proj, _run = "demo-org", "demo-project", "demo-run-1"
    # _facade = build_local_facade(_runs)
    #
    # projection = facade_status(
    #     _facade,
    #     subject_id=_org, organization_id=_org,
    #     project_id=_proj, run_id=_run,
    # )
    # print(f"run_state: {projection.run_state}")
    #
    # # To resume a paused connector run (re-supply prompts for active nodes):
    # projection = facade_resume(
    #     _facade,
    #     subject_id=_org, organization_id=_org,
    #     project_id=_proj, run_id=_run,
    #     node_prompts={"agent": "Summarise AI reliability engineering, 100 words."},
    # )
    # print(f"run_state after resume: {projection.run_state}")
    #
    # # To record an approval decision for an approval-node graph:
    # projection = facade_approve(
    #     _facade,
    #     subject_id=_org, organization_id=_org,
    #     project_id=_proj, run_id=_run,
    #     node_id="approval-checkpoint",
    #     decision="approved",
    # )
    # print(f"run_state after approve: {projection.run_state}")

    print("graph_runtime_reference: module import OK; no execution performed.")
