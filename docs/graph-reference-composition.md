# Graph Engine — Reference Composition

This document explains the reference wiring in
`examples/graph_runtime_reference.py`: the two connector modes, the runtime
facade, the MCP surface, and the deployment seams a client must supply.

---

## Two connector modes

The graph engine runs connector nodes by two transports.  A single graph may
contain nodes of both kinds; the engine routes each node to the right worker
by its binding transport.

### Local-CLI (default)

`transport: local_cli` nodes invoke the user's own subscription-mode agent CLI
as a subprocess.  The engine never reads or stores credentials; the CLI
authenticates out-of-band.  Network access is `OPEN` for these nodes so the
CLI can reach its model and tools.

Supported provider IDs: `claude`, `codex`, `grok`, `muse`, `agy`.  Any other
`provider_id` fails the node closed at execution time with a named error.

### BYOK/https

`transport: https` nodes go through the real `HttpConnectorForwarder` (wired
via `ConnectorInvoker` + `OpaqueCredentialBroker`).  The deployment supplies
an `AdmittedConnectionRecord` per connection; the record carries the endpoint
host and the name of the ENV VAR that holds the credential — not the
credential value.  The engine resolves the credential from the environment at
run time via `EnvCredentialResolver`.

The `EgressBroker` enforces a single-use, time-bound, effect-bound lease on
every outbound request and blocks SSRF/DNS-rebind attempts (private, loopback,
link-local, and CGNAT ranges are denied).

Preflight fails closed if an https node has no matching admitted record.  The
isolation floor for https nodes is `container_restricted` (lifted automatically
if the node declared a lower tier).

**To run either mode via the CLI:**

```bash
# Local-CLI
bl graph run --execute manifest.yaml \
  --connections connections.json --inputs inputs.json --out ./run

# BYOK/https (additional flag)
bl graph run --execute manifest.yaml \
  --connections connections.json --inputs inputs.json \
  --admitted admitted.json --out ./run
```

---

## LocalGraphRuntimeFacade

`LocalGraphRuntimeFacade` is the concrete `GraphRuntimeFacade` implementation
for persisted local run directories.  It wires the real arena reads, real
connector workers, and the `approvals.approve` use case through file-based
local adapters.

Construct it once; the facade is re-used across calls:

```python
from pathlib import Path
from bounded_loops.graph.application.graph_runtime_facade import (
    LocalGraphRuntimeFacade,
    SameTenantArenaAuthorizer,
)

facade = LocalGraphRuntimeFacade(
    runs_root=Path("/var/bl-runs"),
    arena_authorizer=SameTenantArenaAuthorizer(),
)
```

Run directory layout (written by `execute_graph_run` or `bl graph run --execute`):

```
runs_root / <org> / <project> / <run_id> /
    plan.json               — canonical execution plan bytes
    manifest.yaml           — original authoring manifest
    connections.json        — admitted connection records
    run-meta.json           — org, project, run_id, plan_id, policy_digest
    controller-events.jsonl — hash-chained event log (authority)
    approvals.json          — durable approval decision records (written by approve)
    artifacts/              — per-node content-addressed artifacts
```

### Three protocol methods

**`status(request)`** — side-effect-free Arena projection.

**`resume(request)`** — re-drives a paused run.  Fails closed if any
non-terminal connector node has no prompt re-supplied in `node_prompts`.
Prompts are not persisted; re-supply on every call.

**`approve(request, *, node_id, decision)`** — records a human decision for an
approval node and resumes the run.  For `"approved"`: validates authority,
persists the decision durably to `approvals.json`, then resumes.  For
`"rejected"`: records in-memory rejection and fails the run closed.

See `examples/graph_runtime_reference.py` for working call patterns for all
three methods, including the immutable-update pattern via `dataclasses.replace`
for supplying per-call prompts without mutating the shared facade.

---

## MCP surface

`mcp_graph.register(mcp, facade, *, subject_provider)` wires four tools onto a
FastMCP instance.  The module requires `pip install bounded-loops[mcp]` and
must not be imported at module load time in code that does not need it.

The four tools:

| Tool | Side effects | confirm gate |
|---|---|---|
| `graph_status_tool` | none | — |
| `graph_state_md_tool` | none | — |
| `graph_resume_tool` | resumes the run | `confirm=False` → preview |
| `graph_approve_tool` | records decision + resumes | `confirm=False` → preview |

`confirm=False` (the default) returns a preview dict describing what would
happen; `confirm=True` applies.  This is a per-call affordance — proportionate
to run-control's bounded blast radius.

**Subject binding is non-negotiable:** `subject_id` is derived per call from
`subject_provider`, which the deployment wires to the MCP session's
authenticated identity.  It is never an LLM tool argument.  The LLM controls
only intent (which org, project, run, node); it cannot spoof the authenticated
actor.

A minimal wiring pattern:

```python
# deployment entrypoint — NOT at import time
import fastmcp
from bounded_loops.graph.mcp_graph import register

mcp = fastmcp.FastMCP("bounded-loops-graph")
register(mcp, facade, subject_provider=lambda: session.authenticated_org_id)
```

---

## Deployment seams

These are the components a client must supply.  The engine provides everything
else.

| Seam | What to provide |
|---|---|
| Agent CLI binaries | Install and log in to `claude`, `codex`, `grok`, `muse`, and/or `agy` on the host |
| BYOK admitted-connection records | A JSON file mapping `connection_id` to endpoint + credential ENV-VAR name (no secrets); passed as `--admitted` on the CLI or `admitted_connections` in Python |
| MCP subject provider | A function that returns the authenticated org/subject from the MCP session; never sourced from LLM arguments |
| Hosted `ApprovalAuthorizationPort` | A role-checking authorizer; the local default (`SameTenantApprovalAuthorizer`) accepts same-tenant subjects without role verification |
| Hosted `ApprovalSignatureVerifierPort` | A crypto verifier; the local default accepts any non-empty signature (MCP session is the auth boundary for local runs) |
| Hosted `ArenaReceiptVerifierPort` | A remote hash-chain verifier; the local default is a no-op (`_NoopArenaReceiptVerifier`) |
| Durable memory adapters | An SLM-backed `GraphMemoryStorePort` / `SemanticMemoryStorePort` for cross-run memory persistence |
| `AuditStorePort` | A concrete audit store (e.g., `LocalAuditStore`) for persisting audit plans |

---

## What is not yet available

- Approval nodes in `bl graph run --execute`: refused at preflight with a named
  message.  MCP-driven approve via `LocalGraphRuntimeFacade.approve` is shipped.
- Durable approval load on resume: `resume` does not reload previously persisted
  `approvals.json` entries into the resolver; a re-approved run must call
  `approve` again (known follow-up).
- Durable rejection persistence: `"rejected"` decisions are recorded in-process
  but not written to `approvals.json` (known follow-up).
- Cross-model audit controller and Arena wiring (later phase).
- Enterprise egress firewall — RC-LOCKDOWN tier (later phase).
- Hosted `ArenaReceiptVerifierPort` — `bl graph status` outputs a
  `LOCAL/UNVERIFIED` notice for all local runs.
