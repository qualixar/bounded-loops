# Graph Engine — Release Readiness Assessment

**Version:** 0.3.1  
**Assessment date:** 2026-08-11  
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

3. **End-to-end Local-CLI execution** for graphs whose nodes bind an admitted
   `local_cli` connection:
   - `bl graph run --execute <manifest> --connections <json> --inputs <json> --out <dir>`
   - Works with `claude`, `codex`, `grok`, `muse`, `agy` (user's own subscription login).
   - Engine never reads or logs credentials; the CLI authenticates out-of-band.
   - Fail-closed preflight before any node runs (approval nodes refused, non-`local_cli`
     transports refused with a named message, unknown provider IDs fail the node, missing
     prompts fail the node, missing CLI binary fails the node).
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

7. **No-secret egress broker** — single-use, time-bound, effect-bound lease authorization
   with SSRF/DNS-rebind protection. The `ConnectorForwardPort` seam is defined; a concrete
   forwarder is a deployment binding.

8. **Cross-model audit engine** — `AuditPlanService` (coverage validation, content-addressed
   plan persistence) and `reconcile_audit` (highest-severity-preserving, dissent-flagging
   multi-lane reconciliation with full blocking-reason enumeration).

9. **Graph runtime over MCP** — `bounded-loops-mcp` exposes the full graph tool surface
   over MCP. A concrete `GraphRuntimeFacade` is a deployment binding.

---

## What requires the client's own configuration

| Requirement | Detail |
|---|---|
| Agent CLI binaries | `claude`, `codex`, `grok`, `muse`, and/or `agy` must be installed and logged in on the host. The engine ships profiles for these five; no others are recognized. |
| Admission workflow | The `connections.json` file requires valid `admission_digest` and `route_policy_digest` sha256 values. For local development, placeholder hashes (e.g., 64 × `b`) are accepted by the compiler. A production admission workflow is deployment-owned. |
| Durable memory | `InMemoryGraphMemoryStore` is the reference implementation — in-process only. A durable, cross-run memory store (SLM-backed adapter) is a deployment binding. |
| Concrete `ConnectorForwardPort` | Required for BYOK/HTTP connector nodes. Not bundled. |
| Concrete `GraphRuntimeFacade` | Required for the MCP server to serve live runs. Not bundled. |
| `AuditStorePort` implementation | `LocalAuditStore` is in the adapters layer; it must be wired by the deployment. |
| Native sandbox availability | `bl graph run --execute --out <dir>` (sandbox demo) requires macOS Seatbelt or Linux bubblewrap. The command reports honestly if neither is available. |

---

## Not yet available (honest list)

| Capability | Status |
|---|---|
| BYOK/HTTP connector as a run mode | Seam implemented (`connector_forward.py`, `EgressBroker`). Not wired into `bl graph run --execute`. HTTP-transport nodes are refused at preflight with a named message. |
| Human-approval checkpoint execution | `approval` nodes are refused at preflight with a named message. The approval data model and `approvals.py` use case exist. |
| Cross-model audit controller + Arena wiring | Audit plan service and reconciliation are implemented. The wiring into the graph execution flow and Arena projection is explicitly out of scope for the current phase (noted in `audit_plan.py`). |
| Enterprise egress firewall (RC-LOCKDOWN) | The Local-CLI connector's "run freely" posture is the only available tier. The opt-in RC-LOCKDOWN tier is a later phase. |
| Hosted `ArenaReceiptVerifierPort` | The local `_NoOpReceiptVerifier` is used for all local runs. Remote hash-chain verification is a later phase. |
| Sandboxed arbitrary-tool node execution (package broker) | Refused at preflight with a named message. |

---

## Quality posture

| Dimension | State |
|---|---|
| Test suite | ~1454 tests; marked `network`, `external_tool`, `provider_smoke`, and `clean_install` tests are opt-in and excluded from the default `pytest` run |
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
Local-CLI graph execution with any of the five supported agent CLIs, Arena review of run
artifacts, in-process demonstration of the run-directory structure.

**Not ready for production without:** a concrete `GraphRuntimeFacade` (for MCP), a concrete
`ConnectorForwardPort` (for BYOK/HTTP), an `AuditStorePort` wiring, a durable memory adapter,
and a hosted receipt verifier. The BYOK/HTTP connector run mode, human-approval checkpoint
execution, and the enterprise egress firewall are later phases with clear seam boundaries
already defined in the codebase.
