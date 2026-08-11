# Graph Engine — Release Readiness Assessment

**Version:** 0.3.1  
**Assessment date:** 2026-08-11 (updated post-RE/RF shipping)  
**Scope:** `bl graph` subcommand group and supporting application/adapter layers

---

## What a client gets today

A client who installs `bounded-loops` and runs `bl graph` gets:

1. **A portable graph format** (`api_version: "bounded-loops.dev/graph/v1"`) with a
   strict, closed-schema compiler that rejects unknown fields, absolute paths, secret-
   shaped names, cycles, unresolved references, and policy mismatches at compile time.

2. **Five `bl graph` commands that run without any agent credential:**
   - `bl graph lint <manifest>` — validate a manifest, emit digest and field counts.
   - `bl graph plan <manifest> --connections <json>` — compile to an execution plan.
   - `bl graph demo --out <dir>` — in-process DEMONSTRATION (banner-labeled; no isolation
     or E2 enforcement) that exercises the full run-directory structure.
   - `bl graph studio [--from <manifest>] [--out <file.html>]` — self-contained visual
     authoring tool for building and editing graphs.
   - `bl graph arena --run <dir>` — render a persisted run as a self-contained,
     read-only HTML page.

3. **End-to-end connector execution — Local-CLI and BYOK/https** for graphs
   whose nodes bind an admitted `local_cli` or `https` connection (RE shipped):
   - `bl graph run --execute <manifest> --connections <json> --inputs <json> --out <dir>`
   - Local-CLI: works with `claude`, `codex`, `grok`, `muse`, `agy` (user's own
     subscription login). Engine never reads or logs credentials; the CLI authenticates
     out-of-band. Network access is OPEN for admitted `local_cli` nodes.
   - BYOK/https: pass `--admitted <json>` (a map of `connection_id` → endpoint + ENV-VAR
     name record). The engine routes https-transport nodes through `HttpConnectorForwarder`
     + `EgressBroker` (no-secret, single-use, time-bound leases, SSRF/DNS-rebind denied).
     Isolation floor for https nodes is `container_restricted`. Preflight fails closed if
     an https node has no matching admitted record.
   - A single graph may mix both transports; a `_ByokDispatchWorker` routes by binding.
   - Fail-closed preflight before any node runs (approval nodes refused with a named
     message, unknown provider IDs fail the node, missing prompts fail the node, missing
     CLI binary fails the node).
   - Independent structural acceptance gate per node.
   - Hash-chained `controller-events.jsonl` + content-addressed artifact store.
   - Plan reconstruction verification before any status or arena read.

4. **Native OS sandbox demo** — `bl graph run --execute --out <dir>` (no manifest)
   runs a built-in probe inside macOS Seatbelt or Linux bubblewrap and proves the OS
   denied the network and out-of-workspace writes via an independent gate.

5. **`bl graph status --run <dir>`** — text projection of the event log with an honest
   `LOCAL/UNVERIFIED` notice.

6. **Memory spine** — exact KV (`GraphMemoryStorePort`) and semantic recall
   (`SemanticMemoryStorePort`) ports, with a reference in-memory implementation and an
   SLM-backed adapter as a deployment binding.

7. **No-secret egress broker and HTTP forwarder** — `EgressBroker`: single-use, time-bound,
   effect-bound lease authorization with SSRF/DNS-rebind protection. `HttpConnectorForwarder`
   is now bundled as the concrete `ConnectorForwardPort` implementation for https-transport
   nodes (wired by the BYOK/https run mode in RE).

8. **Cross-model audit engine** — `AuditPlanService` (coverage validation, content-addressed
   plan persistence) and `reconcile_audit` (highest-severity-preserving, dissent-flagging
   multi-lane reconciliation with full blocking-reason enumeration).

9. **Graph runtime over MCP** — `bounded-loops-mcp` exposes the full graph tool surface
   over MCP (RF shipped). `LocalGraphRuntimeFacade` is now bundled: a concrete,
   file-backed `GraphRuntimeFacade` that wires real arena reads, real connector workers,
   and the `approvals.approve` use case. `SameTenantArenaAuthorizer` is bundled for
   same-tenant local runs. Approval authority (authorizer + signature verifier) is
   injectable; local defaults gate on same-tenant subject + non-empty signature (MCP
   session is the auth boundary). `mcp_graph.register(mcp, facade, subject_provider=...)`
   wires four MCP tools: `graph_status_tool`, `graph_state_md_tool`, `graph_resume_tool`,
   `graph_approve_tool` (mutating tools gated by `confirm=True`). Subject identity comes
   from `subject_provider` — never from LLM arguments. See
   `examples/graph_runtime_reference.py` and `docs/graph-reference-composition.md`.

---

## What requires the client's own configuration

| Requirement | Detail |
|---|---|
| Agent CLI binaries | `claude`, `codex`, `grok`, `muse`, and/or `agy` must be installed and logged in on the host. The engine ships profiles for these five; no others are recognized. |
| Admission workflow | The `connections.json` file requires valid `admission_digest` and `route_policy_digest` sha256 values. For local development, placeholder hashes (e.g., 64 × `b`) are accepted by the compiler. A production admission workflow is deployment-owned. |
| BYOK admitted-connection records | For https-transport nodes: a JSON file mapping `connection_id` → `AdmittedConnectionRecord` (endpoint host + credential ENV-VAR name — never the credential value itself). Passed as `--admitted` on the CLI or `admitted_connections` in Python. |
| Hosted `ApprovalAuthorizationPort` | `LocalGraphRuntimeFacade` defaults to same-tenant subject authorization (no role check). A hosted deployment injects a role-checking authorizer. |
| Hosted `ApprovalSignatureVerifierPort` | `LocalGraphRuntimeFacade` defaults to accepting any non-empty signature (MCP session is the auth boundary for local runs). A hosted deployment injects a crypto verifier. |
| Hosted `ArenaReceiptVerifierPort` | `LocalGraphRuntimeFacade` uses a no-op verifier. A hosted deployment injects a verifier that checks hash chains against a remote server. |
| MCP `subject_provider` | The deployment must provide a function that reads the authenticated subject from the MCP session. It must never accept a subject from LLM tool arguments. |
| Durable memory | `InMemoryGraphMemoryStore` is the reference implementation — in-process only. A durable, cross-run memory store (SLM-backed adapter) is a deployment binding. |
| `AuditStorePort` implementation | `LocalAuditStore` is in the adapters layer; it must be wired by the deployment. |
| Native sandbox availability | `bl graph run --execute --out <dir>` (sandbox demo) requires macOS Seatbelt or Linux bubblewrap. The command reports honestly if neither is available. |

---

## Not yet available (honest list)

| Capability | Status |
|---|---|
| Human-approval checkpoint via `bl graph run --execute` | `approval` nodes are refused at preflight in `execute_graph_run` with a named message. MCP-driven approve via `LocalGraphRuntimeFacade.approve` IS shipped (RF). |
| RF follow-up — durable approval load on resume | `LocalGraphRuntimeFacade.resume` does not reload previously persisted `approvals.json` entries into the resolver. A re-approved run must call `approve` again (known follow-up). |
| RF follow-up — durable rejection persistence | `"rejected"` decisions are recorded in-process but not persisted to `approvals.json`. A session restart after a rejection loses the rejection record (known follow-up). |
| Cross-model audit controller + Arena wiring | Audit plan service and reconciliation are implemented. The wiring into the graph execution flow and Arena projection is explicitly out of scope for the current phase (noted in `audit_plan.py`). |
| Enterprise egress firewall (RC-LOCKDOWN) | The Local-CLI connector's "run freely" posture is the only available tier. The opt-in RC-LOCKDOWN tier that restricts what the CLI can reach is a later phase. |
| Hosted `ArenaReceiptVerifierPort` | `LocalGraphRuntimeFacade` uses `_NoopArenaReceiptVerifier` for all local runs. `bl graph status` outputs a `LOCAL/UNVERIFIED` notice. Remote hash-chain verification is a later phase. |
| Sandboxed arbitrary-tool node execution (package broker) | Refused at preflight with a named message. |

---

## Quality posture

| Dimension | State |
|---|---|
| Test suite | ~1485 tests; marked `network`, `external_tool`, `provider_smoke`, and `clean_install` tests are opt-in and excluded from the default `pytest` run |
| Linting | `ruff` clean |
| Type checking | `mypy` clean |
| File size cap | 800-line hard cap per file (enforced by convention; the CLI graph handler splits handlers across sibling files to stay within budget) |
| Design invariant | Immutable domain objects; receipt-first; the event log is authority, never working state |
| Audit rhythm | Dual cross-model audit: `AuditPlanService` (plan with independent coverage) + `reconcile_audit` (multi-lane, highest-severity-preserving) |
| IP leakage | Portable graphs reject secret-shaped fields and absolute paths at the schema layer; the CLI connector never reads credentials; run-time prompts are not persisted |
| Fail-closed pattern | Every boundary that cannot proceed refuses explicitly and exits non-zero; no silent skip |

---

## Summary verdict

**Ready for:** local development, CI/CD integration, graph authoring + linting + compilation,
Local-CLI and BYOK/https connector execution with any of the five supported agent CLIs or an
admitted https endpoint, MCP-driven run status/resume/approve via `LocalGraphRuntimeFacade`,
Arena review of run artifacts, in-process demonstration of the run-directory structure.

**Not ready for production without:** hosted authorization ports (`ApprovalAuthorizationPort`,
`ApprovalSignatureVerifierPort`, `ArenaReceiptVerifierPort`) replacing the local defaults, an
`AuditStorePort` wiring, a durable memory adapter, and the RF follow-ups (durable approval load
on resume, durable rejection persistence). The cross-model audit controller/Arena wiring,
human-approval execution via `bl graph run --execute`, and the enterprise egress firewall
(RC-LOCKDOWN) are later phases with clear seam boundaries already defined in the codebase.
