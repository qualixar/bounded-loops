# Graph Engine — Release Readiness Assessment

**Version:** 0.6.0  
**Assessment date:** 2026-08-15 (updated for the 0.6.0 release: MCP 2.0, `bl monitor`, and a
third round of dual external audit — five focused passes per auditor)  
**Scope:** `bl graph` subcommand group and supporting application/adapter layers

**What the third audit round changed, and why it belongs in a readiness document.** Eight HIGH
findings survived independent verification. One was that `bl_run(confirm=true)` could not
execute over MCP at all: the preview/confirm handshake keyed on a session object that MCP 2.0
rebuilds per request. The tool's primary function was unreachable on its shipped transport,
and the test guarding it asserted that `Context.session` existed — which was true, and was
never the property the handshake needed.

Five of the remaining seven were surfaces asserting more than the receipt log supported: a
gate-pass badge on nodes no gate had judged, "all nodes succeeded" printed above a table
showing a skipped branch, a publish reported as undoable, an approval preview that never named
what approving released, and `bl graph status` refusing runs it had just watched succeed.

The pattern is worth stating for anyone assessing this engine: none of these were failures of
the execution core. The receipts were right every time. What drifted were the things that
read them.

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
     out-of-band. Network access for `local_cli` nodes uses the configurable egress
     posture: OPEN (default) or ALLOWLIST (opt-in via `bl graph init` — see item 10a).
   - BYOK/https: pass `--admitted <json>` (a map of `connection_id` → endpoint + ENV-VAR
     name record). The engine routes https-transport nodes through `HttpConnectorForwarder`
     + `EgressBroker` (no-secret, single-use, time-bound leases, SSRF/DNS-rebind denied).
     Isolation floor for https nodes is `container_restricted`. Preflight fails closed if
     an https node has no matching admitted record.
   - A single graph may mix both transports; a `_ByokDispatchWorker` routes by binding.
   - Fail-closed preflight before any node runs (approval nodes SKIP preflight and PAUSE
     at execution — exit code 3; see item 10b; unknown provider IDs fail the node, missing
     prompts fail the node, missing CLI binary fails the node).
   - Independent structural acceptance gate per node. The gate verdict and
     artifact digest are **co-recorded in the same hash-chained event** (structural
     binding via co-location; the default `StructuralAcceptanceGate` does not
     populate `GateVerdict.evidence_digest`).
   - Hash-chained `controller-events.jsonl` + content-addressed artifact store.
     On every `resume`, the complete event log hash chain is re-verified in full,
     and artifact bytes are re-verified on every `open()`. SUCCEEDED nodes are not
     re-gated on resume — resume trusts recorded verdicts if the hash chain is intact.
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
   multi-lane reconciliation with full blocking-reason enumeration). The read side is wired
   as a runnable mode (C-079): `bl graph run --execute --audit-plan <json>` persists the
   plan alongside the run, and `bl graph arena` computes and renders the coverage table +
   release verdict, failing closed if the projection cannot be computed. Independence is
   receipt-**asserted** (the assessor is never the producer); structurally binding a
   coverage cell to the auditor's model_id and receipt route (write-side independence) is
   still deferred.

9. **Graph runtime over MCP** — the graph engine's MCP tools are wired by the
   `mcp_graph.register(...)` shim onto a deployment-provided server (RF); the shipped
   `bounded-loops-mcp` server itself exposes the loop tools only, not the graph tools.
   `LocalGraphRuntimeFacade` is now bundled: a concrete,
   file-backed `GraphRuntimeFacade` that wires real arena reads, real connector workers,
   and the `approvals.approve` use case. `SameTenantArenaAuthorizer` is bundled for
   same-tenant local runs. Approval authority (authorizer + signature verifier) is
   injectable; local defaults gate on same-tenant subject + non-empty signature (MCP
   session is the auth boundary). `mcp_graph.register(mcp, facade, subject_provider=...)`
   wires four MCP tools: `graph_status_tool`, `graph_state_md_tool`, `graph_resume_tool`,
   `graph_approve_tool` (mutating tools gated by `confirm=True`). Subject identity comes
   from `subject_provider` — never from LLM arguments. Both `approve` and `reject`
   decisions are persisted durably to `approvals.json` (exclusive `flock` + atomic
   `os.replace`) and rehydrated on `resume` (C-080), so a crash between a human decision
   and the next resume is recovered from the ledger, not re-asked. See
   `examples/graph_runtime_reference.py` and `docs/graph-reference-composition.md`.

10a. **Egress posture configuration — `bl graph init`**: interactive installer that writes
    `~/.bounded-loops/egress.json` atomically (unique temp + O_EXCL + O_NOFOLLOW + fchmod
    0600 + fsync + round-trip verify + os.replace). OPEN is the default (no cage, no config
    file required). ALLOWLIST (macOS Seatbelt cage for `local_cli` nodes) is opt-in.
    Fail-closed: ALLOWLIST without Seatbelt raises `GraphValidationError` at preflight,
    never silently falls back to OPEN. BROKER is refused for `local_cli` nodes
    (architecturally incoherent). ALLOWLIST is a network-only cage — filesystem access
    (HOME/TMPDIR/workdir) is unchanged.

10b. **Human-approval checkpoint via `bl graph run --execute`**: approval nodes now PAUSE the
    run (exit code 3 AWAITING_APPROVAL) instead of refusing at preflight. Use `bl graph
    approve --run <dir> --node <id> --decision {approved,rejected}` to record the decision
    and resume. `LocalGraphRuntimeFacade.for_run_dir(run_dir)` addresses runs by flat path.
    Shared `build_durable_approval_resolver` provides the same durable persistence and
    rehydration as the MCP path. Local posture: same-tenant authorizer + non-crypto signature
    verifier; hosted deployments must inject their own ports.

10c. **Click-to-approve console — `bl graph console --run <dir>`**: loopback-only HTTP server
    (127.0.0.1, never 0.0.0.0) serving a single-page approval UI. Per-invocation
    `secrets.token_urlsafe(32)` token; CSRF defense via Origin header check; 8 KB body cap;
    30-second handler timeout; HTTP/1.0. Auto-stops after the page is served (from GET
    handler, not POST). LOCAL posture: the token gates other local processes on the same host
    — a hosted deployment needs TLS + real auth + role-checking authorizer.

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
| Reject-path signature-gating for hosted deployments | `bl graph approve --decision rejected` is accepted on filesystem writability for local posture. A hosted multi-tenant deployment must inject a crypto `ApprovalSignatureVerifierPort` to gate reject identically to approve. |
| Cross-model audit controller + Arena wiring — write side | Audit plan service, reconciliation, and the read-side controller→Arena wiring (coverage table + release verdict via `bl graph arena`) are implemented and shipped (C-079). Structurally binding a coverage cell to the auditor's `model_id` and receipt route — write-side independence, beyond today's receipt-asserted independence — is still deferred. |
| ALLOWLIST as the default connector tier (RC-LOCKDOWN) | ALLOWLIST is shipped and opt-in for `local_cli` nodes via `bl graph init` or env var (C-081). Making it the default tier for all connector nodes is deferred. |
| Hosted `ArenaReceiptVerifierPort` | `LocalGraphRuntimeFacade` uses `_NoopArenaReceiptVerifier` for all local runs. `bl graph status` outputs a `LOCAL/UNVERIFIED` notice. Remote hash-chain verification is a later phase. |
| Sandboxed arbitrary-tool node execution (package broker) | Refused at preflight with a named message. |

---

## Quality posture

| Dimension | State |
|---|---|
| Test suite | 1527 passed, 7 skipped, 30 deselected; marked `network`, `external_tool`, `provider_smoke`, and `clean_install` tests are opt-in and excluded from the default `pytest` run |
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
admitted https endpoint, human-approval checkpoints via `bl graph approve` or `bl graph console`,
opt-in ALLOWLIST egress posture via `bl graph init`, MCP-driven run status/resume/approve via
`LocalGraphRuntimeFacade`, Arena review of run artifacts, in-process demonstration of the
run-directory structure.

**Not ready for production without:** hosted authorization ports (`ApprovalAuthorizationPort`,
`ApprovalSignatureVerifierPort` — reject-path gating for hosted deployments — and
`ArenaReceiptVerifierPort`) replacing the local defaults, an `AuditStorePort` wiring, and a
durable memory adapter. Write-side structural independence for the cross-model audit controller
and the enterprise egress firewall (RC-LOCKDOWN) as the default connector tier are later phases
with clear seam boundaries already defined in the codebase.
