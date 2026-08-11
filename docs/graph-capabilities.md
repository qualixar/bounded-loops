# Graph Engine — Capabilities and Boundaries

This document describes what the graph engine does today, what is wired as a seam
for a later phase, and where a deploying engineer needs to provide their own binding.

---

## What ships now

### 1. Portable graph authoring and strict compiler

A graph is a YAML or JSON manifest (`api_version: "bounded-loops.dev/graph/v1"`).
The compiler validates and rejects:

- Unknown fields at every node and slot (closed schema — no extra keys allowed).
- Absolute paths and secret-shaped field names (`api_key`, `token`, `password`, etc.)
  anywhere in the graph.
- Node kinds whose provider is baked in (`provider_in_slot` error — connection slots
  declare capabilities, never provider names).
- Cycles in the edge graph.
- References to undeclared slots, ports, or nodes.

Node kinds: `research_claim`, `research_source`, `audit`, `tool`, `loop`, `subgraph`,
`router`, `join`, `approval`, `publish`.

Isolation levels: `workspace_only`, `process_restricted`, `container_restricted`,
`customer_managed_worker`.

Effects: `read_only`, `workspace_write`, `external_write`, `financial`, `irreversible`.

Data classes: `public`, `internal`, `confidential`, `restricted`.

The compiler enforces that an admitted connection's capabilities, allowed effects,
isolation rank, and data-class rank all satisfy the node's declared requirements.
A node that no admitted connection satisfies fails closed at compile time — never at
run time.

**CLI:**
```bash
bl graph lint manifest.yaml           # validate only
bl graph plan manifest.yaml --connections connections.json   # compile to plan
```

---

### 2. Native OS sandbox execution (no Docker)

`bl graph run --execute --out <dir>` (no manifest) runs the built-in sandbox probe
node inside a native OS sandbox:

- macOS: Seatbelt (`sandbox-exec`)
- Linux: bubblewrap (`bwrap`)

No Docker daemon, no root, no container registry. The sandbox probe attempts a
network connection and an out-of-workspace write; an **independent gate** then reads
the artifact and PASSES only if the OS actually denied both. The node never grades
itself.

Platform detection is automatic via `probe_platform()`. If neither sandbox mechanism
is available, the command reports that honestly rather than silently skipping isolation.

---

### 3. Local-CLI connector — end-to-end execution

`bl graph run --execute manifest.yaml --connections connections.json --inputs inputs.json --out <dir>`
runs graphs whose nodes bind an admitted `local_cli` connection.

The connector invokes the user's own subscription-mode agent CLI as a subprocess:

| `provider_id` in `connections.json` | CLI invoked | Prompt delivery |
|---|---|---|
| `claude` | `claude -p` | stdin |
| `codex` | `codex exec --skip-git-repo-check` | positional arg |
| `grok` | `grok -p` | positional arg |
| `muse` | `muse exec` | positional arg |
| `agy` | `agy -p` | positional arg |

The engine never reads, stores, or logs the user's credentials. The CLI
authenticates out-of-band via its own login session. Network access is OPEN for
admitted `local_cli` nodes so the CLI can reach its model and tools. An admitted
`https` node gets its own network mode instead (`ALLOWLIST`, isolation lifted to
`container_restricted` — see item 8). Any node that is neither is refused at
preflight and never reaches a network-mode decision.

The run-time prompt (`inputs.json`: `node_id -> prompt string`) is not persisted in
the run directory. A prompt may contain a secret; the content-addressed reply
artifact is the durable receipt.

**Fail-closed preflight** (checked before any node runs):
- `approval` nodes are refused — human-approval execution via `--execute` is a later
  phase (the facade/MCP path already supports it durably; see item 8 below).
- Any node whose binding is neither `local_cli` nor `https` (see item 8) is refused
  with an explicit message naming which phase will handle it — e.g. sandboxed tool
  execution.
- An unknown `provider_id` (not in the five profiles above) fails the node closed.
- A missing prompt for a node fails the node closed.
- A CLI binary not found in PATH fails the node closed.

---

### 4. Hash-chained receipts and replay

Every run writes a `controller-events.jsonl` event log tied to a `GraphRunIdentity`
(organization, project, run, graph digest, plan digest, policy digest). The event log
is append-only; each entry is chained to the previous by digest.

The run directory contains `manifest.yaml`, `connections.json`, `plan.json`, and
`run-meta.json`. `bl graph status` and `bl graph arena` reconstruct the full plan from
these files and verify that the reconstructed `plan_id` matches the stored one before
reading any events. A tampered manifest or connections file produces a mismatch and
the command refuses to continue.

---

### 5. Read-only Arena UI

```bash
bl graph arena --run ./my-run [--out arena.html]
```

Generates a self-contained, static HTML page from the persisted run directory.
The Arena is read-only — it renders node states, artifact digests, routes, and
the event log. It does not connect to any server; all data is embedded in the HTML.

```bash
bl graph status --run ./my-run [--json]
```

Prints the same projection to stdout. Output includes a `LOCAL/UNVERIFIED` notice:
the projection is derived from the local event log, not verified against a hosted
server.

---

### 6. STATE.md

The `render_state_markdown` function produces a Markdown projection of any
`ArenaProjection` — the same content the Arena renders, as Markdown. STATE.md is a
read-only UX projection. It is never authority: the event log and receipts are the
source of truth. A run directory rendered to STATE.md is a convenience snapshot, not
a substitutable record.

---

### 7. Memory spine — durable KV and semantic recall

The graph runtime provides two distinct memory ports:

**Exact KV** (`GraphMemoryStorePort`): tenant-scoped, namespaced working memory.
Put, get, search by namespace, delete. Values must be JSON-round-trippable and under
256 KB. One store per `(organization, project)` — no cross-tenant API. The reference
implementation is `InMemoryGraphMemoryStore`; a durable SLM-backed adapter satisfies
the same port and is a deployment binding.

**Semantic recall** (`SemanticMemoryStorePort`): recall by meaning, not by exact key.
Returns ranked results for a natural-language query within a namespace. Backed by SLM
(`remember` / `recall`). Callers must treat results as hints, not facts of record —
the event log is authority.

Both ports are UX / working state per ADR-12 D4. A memory value can never substitute
for a receipt.

---

### 8. No-secret egress broker and BYOK HTTP forwarder seam

The `EgressBroker` authorizes one outbound request behind a single-use, time-bound,
effect-bound credential lease. It never hands a credential value to a node. Properties:

- Single-use: a `lease_id` authorizes exactly one request atomically; a denied
  request does not burn the lease.
- Time-bound: an expired lease is refused.
- Effect-bound: the request's effect must be one the lease grants.
- SSRF/DNS-rebind protection: the destination host is resolved once; every resolved
  address must be a globally-routable public unicast address. Private, loopback,
  link-local (including 169.254.169.254), CGNAT, multicast, and reserved ranges are
  denied. The broker returns pinned addresses; the forwarder connects only to those.

The `ConnectorForwardPort` is a `Protocol`; the engine bundles `HttpConnectorForwarder`
as its concrete implementation, paired with `EnvCredentialResolver` (resolves the
credential from an environment variable named in the `AdmittedConnectionRecord` — never
the credential value itself). A deployment may substitute its own forwarder/resolver
(for example, keychain- or KMS-backed) but does not have to. Either way, the forwarder
connects only to the broker's pinned addresses; no credential value and no
request/response bytes pass through the broker itself.

**Shipping status**: SHIPPED. The broker, its SSRF protection logic, and the BYOK/HTTP
connector as a run mode are implemented, tested, and wired end-to-end — `bl graph run
--execute manifest.yaml --admitted admitted.json ...` routes `https`-transport nodes
through this stack via `_ByokDispatchWorker` (`execute_graph.py:1-15,128,247`).
Preflight fails closed if an `https` node has no matching `AdmittedConnectionRecord`
(`execute_graph.py:267-277`).

---

### 9. Graph runtime over MCP

`bounded-loops-mcp` exposes the graph runtime over MCP. The MCP tools validate inputs,
enforce tenant scoping, and translate calls into the graph use cases behind an injected
`GraphRuntimeFacade`. The MCP shim is engine-agnostic: it imports no workers, isolation
adapters, or connectors. Read tools are side-effect-free. Mutating tools (`resume`,
`approve`) gate on a `confirm` flag — `confirm=False` returns a preview and changes
nothing; `confirm=True` applies.

The MCP server never wires real workers, isolation, or connectors. The deployment
provides the `GraphRuntimeFacade`. Subject identity comes from the MCP session, not
from LLM arguments — a caller cannot spoof the authenticated subject.

---

### 10. Cross-model audit engine

**AuditPlanService**: validates audit coverage and persists plans. An `AuditPlan`
assigns each artifact cell to at least one independent auditor — the producer is never
the sole auditor of its own output. Plans are content-addressed by digest.

**`reconcile_audit`**: collapses multiple independent audit results per cell into a
release decision. Rules:
- Preserves the highest severity seen per cell (never lowers a finding).
- Flags DISSENT when lanes disagree.
- A release passes only when every mandatory cell has independent coverage AND carries
  no unresolved S0 or S1 finding.
- Reports every blocking reason in one pass (not first-failure).

The `ReleaseDecision` is advice for a human release owner per LLD 06. The event log
and receipts remain authority.

---

## Documented seams — partial or narrower-than-production wiring

Each of these has a real, tested mechanism in the codebase already. What is listed here
is the specific gap between that mechanism and full production wiring — not an absence
of the mechanism itself.

### Concrete GraphRuntimeFacade wiring

`mcp_graph.py` defines the `GraphRuntimeFacade` `Protocol` and the full MCP tool
surface. `LocalGraphRuntimeFacade` is bundled as the reference concrete implementation
(`graph_runtime_facade.py`): it wires real arena reads, real connector workers
(`build_execution_controller`), and the real `approvals.approve` use case for persisted
local run directories. Its approval authorizer and signature verifier default to
same-tenant-subject and non-empty-signature checks — a hosted, multi-tenant deployment
still must supply a role-checking `ApprovalAuthorizationPort` and a crypto
`ApprovalSignatureVerifierPort` for production-grade authority.

### Cross-model audit controller and Arena wiring — write side

The audit plan service and reconciliation logic are implemented, and the **read side is
wired as a runnable mode** (C-079): `bl graph run --execute --audit-plan <json>`
persists the plan alongside the run, and `bl graph arena` computes the coverage table
and release verdict from it, failing closed if the projection cannot be computed
(`cli_graph.py:858`, `execute_graph.py:596-617`, `cli_arena.py:70-90`). Independence is
receipt-**asserted** — the assessor is never the producer. What remains deferred is the
**write side**: structurally binding a coverage cell to the `AuditAssignment.model_id`
and its succeeded-receipt route, rather than asserting independence from the plan alone.

### Enterprise egress firewall (RC-LOCKDOWN) as the default connector tier

The mechanism — `NetworkMode.ALLOWLIST`, a loopback-only OS cage plus a destination-
allowlisted CONNECT proxy (`enforcer.py:25`, `providers/native.py`, `sandbox.py`,
`egress_proxy.py`) — is shipped and proven live on macOS Seatbelt: a caged process can
reach only the proxy; everything else comes back `denied_by_sandbox`. Linux/docker/bwrap
fail closed today (authorized egress is refused, never faked) rather than caging. It is
exercised today via the native-sandbox probe path (`sandbox_demo.py`), not by connector
nodes: `bl graph run --execute` still runs `local_cli` nodes network-`OPEN` and `https`
nodes broker-mediated, neither OS-caged. Making `ALLOWLIST` the default tier for
connector nodes is the later-phase item — the cage itself is not.

### Hosted ArenaReceiptVerifierPort

`bl graph status` and `bl graph arena` use a no-op receipt verifier for local runs.
A hosted `ArenaReceiptVerifierPort` that verifies the hash chain against a remote
server is a later phase. The `LOCAL/UNVERIFIED` notice in `bl graph status` output
is the honest statement of this posture.

---

## What a deploying engineer must provide

| Binding | What to provide |
|---|---|
| Agent CLI binaries | Install and log in to `claude`, `codex`, `grok`, `muse`, and/or `agy` on the host |
| BYOK admitted-connection records | A JSON file mapping `connection_id` to endpoint + credential ENV-VAR name (no secrets), passed via `--admitted` |
| `ConnectorForwardPort` (optional override) | The engine bundles `HttpConnectorForwarder` + `EnvCredentialResolver` by default; supply your own only for keychain/KMS-backed credential resolution instead of an env var |
| `GraphRuntimeFacade` (hosted-grade override) | `LocalGraphRuntimeFacade` ships as the reference concrete facade; a hosted deployment supplies a role-checking `ApprovalAuthorizationPort` and a crypto `ApprovalSignatureVerifierPort` in its place |
| Durable memory adapters | An SLM-backed `GraphMemoryStorePort` and `SemanticMemoryStorePort` for cross-run memory persistence |
| `AuditStorePort` | A concrete audit store (e.g., `LocalAuditStore`) for persisting audit plans |
| Hosted receipt verifier | A `ArenaReceiptVerifierPort` implementation if you want the Arena to verify hash chains against a remote server |
