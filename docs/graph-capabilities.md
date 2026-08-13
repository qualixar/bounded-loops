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
authenticates out-of-band via its own login session. Network access for admitted
`local_cli` nodes is resolved via the configurable egress posture (see item 11):
OPEN by default (no cage), or ALLOWLIST (macOS Seatbelt cage, opt-in). An admitted
`https` node gets its own network mode (`ALLOWLIST`, isolation lifted to
`container_restricted` — see item 8). Any node that is neither `local_cli` nor
`https` is refused at preflight and never reaches a network-mode decision.

The run-time prompt (`inputs.json`: `node_id -> prompt string`) is not persisted in
the run directory. A prompt may contain a secret; the content-addressed reply
artifact is the durable receipt.

**Conditional edges (`when`)** — an edge fires only when its SOURCE node reached the
outcome the edge names. Four values, and nothing else:

| `when` | The edge applies when its source | Use |
|---|---|---|
| `null` (default) | SUCCEEDED | ordinary data dependency; identical to pre-0.5 behaviour |
| `succeeded` | SUCCEEDED | the same rule, stated explicitly |
| `failed` | FAILED | route around a failure — cleanup, notify, fall back |
| `skipped` | SKIPPED | distinguish "upstream failed" from "upstream never ran" |
| `terminal` | any terminal state | a branch that must run whatever happened |

`failed` means FAILED and nothing else. A run-level stop — a fail-closed halt, an operator
cancel, an expired deadline — satisfies neither `succeeded` nor `failed`, so a recovery
branch does **not** fire on one and the halt propagates. Use `terminal` if you want a
branch to run on those too.

Two rules decide admission, and the distinction between them matters:

- **A condition only ever removes an edge from consideration.** It never overrides the
  join. `all_selected` still tolerates a failed parent and `any_successful` still admits
  as soon as one parent succeeds — those are the join's decisions, not the condition's.
- **An unconditional edge whose dependency failed still blocks.** Only an edge you
  explicitly conditioned can be excluded. This is what keeps a failed dependency from
  becoming a green light when a node has several parents.

A node whose every incoming edge was excluded is marked **SKIPPED** — its branch was not
taken — and the skip cascades to the rest of that branch. A run ends SUCCEEDED when every
node either succeeded or was skipped; a FAILED node still fails the run, even if a
`failed`-conditioned branch handled it.

Data-dependent conditions (`result.status == 'failed'`) are **not** supported: anything
outside the five values above is refused when the graph is validated. Versions up to 0.4.0
accepted such strings and then ignored them, so those edges never applied their condition.

**`failed`, `skipped` and `terminal` require `fail_mode: continue_declared`.** Under
`fail_mode: fail_closed` (the default) the run stops at the *first* node failure, so the
scheduler never gets another turn and a failure-conditioned edge could never be admitted.
Authoring one there is **refused at validation** rather than accepted and silently ignored,
and the error names the mode to switch to.

`continue_declared` keeps driving the graph after a node fails — but only past the node's
own bounded-loop outcome: a gate rejection, a worker fault, an unverified artifact, a spent
retry budget, exhausted re-drives. A **broken gate**, a denied execution policy or isolation
refusal, a missing worker, a rejected or unresolved human approval, an exhausted spend cap, a
broken worker contract, or an unmeasurable budget all still stop the run whatever the mode:
continuing past those would keep spending money, trust a gate that has already proved
unreliable, or route around a control that said no.

An unconditional edge whose dependency failed still blocks in either mode. A run that had any
FAILED node still reports FAILED, even when a `failed`-conditioned branch handled it —
whether a handled failure clears the run is a repair-semantics question, not this one.

The mode is recorded in the run directory's `run-meta.json`, so `resume` and `approve` drive
the graph exactly the way the original run did. A run directory written before the mode was
recorded reduces to `fail_closed`, which is how it originally executed.

**Repair a node upstream (`on_failure: repair`)** — a conditional edge points forward; a
**repair** points backward. When a node exhausts its budget, the ancestor it names runs
again, and everything reachable from that ancestor runs again after it.

```yaml
policies: {fail_mode: continue_declared, repair_budget: 2}
nodes:
  - id: verify
    on_failure: {mode: repair, target: fetch}   # fetch is an ancestor of verify
```

The target is **named**, not implied, because the bound below is only checkable against an
explicit target. The object form is required for `repair`; the other failure policies stay
bare strings.

`repair_budget` is a **global** bound on repair rounds for the whole run — never per node.
That is deliberate: bound repairs per node instead and two nodes can repair each other
forever, each seeing its own counter as unspent. One global counter is what makes the run
provably terminate.

**Total node executions are bounded by `(1 + repair_budget) × Σᵥ max_attemptsᵥ`.**
(`max_attempts` is the total attempts a node may make, so a node with `max_attempts: 1`
contributes 1. In the retry-budget notation used by the scheduling literature, where `bᵥ` is
the number of *retries*, that is `(1 + R) × Σᵥ(bᵥ + 1)` — the same quantity.)
Per-node retry budgets alone do *not* bound a graph with repair — that is the whole point.

Repair is a **bounded outer loop, not a cycle**. Nothing is revived: a round boundary is
recorded as `run.repair.round`, each reset node gets a `node.repaired` receipt naming the
terminal state it left, and every receipt in a round carries that round. Within a round the
state machine is unchanged, so the audit trail stays verifiable — a replay refuses a boundary
whose trigger did not fail, whose failure was not one a run may continue past (a broken gate or a
denied policy cannot be repaired), whose target the trigger did not declare, that is numbered out
of sequence, or that exceeds the budget.

Refused at validation: a target that is not a strict ancestor (including the node itself), a
target that does not exist, `repair` with a `repair_budget` of 0, and `repair` under
`fail_mode: fail_closed` — where the run stops at the first failure, so a repair could never
begin. `on_failure: continue` and `await_human` remain declared-but-unimplemented and are
still refused.

**Fail-closed preflight** (checked before any node runs):
- `approval` nodes are **not refused at preflight** — they are skipped during
  preflight and the run pauses (exit code 3) when execution reaches them.  Use
  `bl graph approve` or `bl graph console` to record a decision and resume (see
  items 12 and 13).
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

**Verdict and artifact digest binding:** The gate verdict and the node's artifact
digest are **co-recorded in the same hash-chained event**. The binding is structural:
both fields appear in the same immutable event record covered by the chain; neither
field cross-references the other outside of that shared record. Specifically, the
default `StructuralAcceptanceGate` does not populate `GateVerdict.evidence_digest` —
the `evidence_digest` field is optional and is only populated when a gate is explicitly
configured to do so.

**Resume verification scope:** On every `resume` call, the complete event log hash
chain is re-verified (`event_log.replay()` re-hashes every prior event), and artifact
bytes are re-verified on every `open()` call against the stored digest. SUCCEEDED
nodes are not re-driven through the gate on resume — the resume trusts recorded gate
verdicts so long as the hash chain covering those events is intact. Incomplete nodes
(those that did not reach SUCCEEDED in the prior run) are re-driven normally.

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

### 11. Egress posture configuration — `bl graph init`

```bash
bl graph init [--posture {open,allowlist,broker}] [--allowlist <host[:port]>] [--yes] [--config <path>]
```

Interactive installer that writes `~/.bounded-loops/egress.json` (or the path in
`--config`).  Defaults: OPEN posture + local_cli connector (frictionless first run).

Atomic write discipline: unique temp file (O_EXCL + O_NOFOLLOW) → `fchmod 0600` →
`fsync` → `verify_round_trip()` (round-reads the temp through the resolver) → `os.replace`.
Refuses symlinks at the config path in every mode.

`--allowlist` is repeatable and comma-ok: `--allowlist api.anthropic.com,api.openai.com`.
`--connector` is informational only — not written to disk (the connector binding lives in
`connections.json`, not the egress config).

Config precedence (highest wins): explicit arg to `decide_egress_posture()` → env var
`BOUNDED_LOOPS_EGRESS_POSTURE` → config file `~/.bounded-loops/egress.json` → default OPEN.

---

### 12. Human approval checkpoints — `bl graph approve`

```bash
bl graph approve --run <dir> --node <id> --decision {approved,rejected} [--inputs <json>] [--json]
```

Records a durable human decision for an approval node and resumes the run.
`LocalGraphRuntimeFacade.for_run_dir(run_dir)` addresses runs by flat path — no
`runs_root` + org/project hierarchy needed.

Durable machinery: `build_durable_approval_resolver` (shared between `execute_graph_run`
and the facade) persists to `approvals.json` under `flock` + atomic `os.replace`.
A bare `resume` after a crash never re-pauses a gate a human already decided.

Exit codes: 0 = SUCCEEDED, 2 = FAILED, 3 = AWAITING_APPROVAL (more approval nodes remain).

**Local posture caveats**: the approve command validates authority via the bundled
`SameTenantApprovalAuthorizer` (same-tenant check, no role verification) and
`SameTenantApprovalSignatureVerifier` (accepts any non-empty signature — the local run
directory is the auth boundary). A hosted, multi-tenant deployment must supply a
role-checking `ApprovalAuthorizationPort` and a crypto `ApprovalSignatureVerifierPort`.
The reject path is not signature-gated locally — reject is accepted on filesystem
writability alone for local posture.

**A decision may be recorded before the node asks for it.** `approve` does not require the
target node to be sitting at `AWAITING_APPROVAL` yet: a decision recorded ahead of time is
persisted and honoured when the run reaches that gate. This is deliberate — it is what lets
a scripted or unattended run carry its approvals with it, and it is how the durable resolver
is exercised end to end. It does mean the approver may not have seen the node's evidence at
the moment they decided, so treat it as a convenience of the local, single-operator posture
rather than a human-in-the-loop guarantee. A hosted, multi-tenant deployment that needs
"the human saw this exact evidence before deciding" must bind each decision to the node's
current hold evidence and reject a decision that arrives early.

---

### 13. Click-to-approve console — `bl graph console`

```bash
bl graph console --run <dir> [--port <n>]
```

Starts a loopback-only HTTP server (binds `127.0.0.1`, never `0.0.0.0`) that serves a
single-page approval UI.  The port defaults to 0 (OS-assigned ephemeral port).

Security properties: per-invocation `secrets.token_urlsafe(32)` token embedded in the
URL; `hmac.compare_digest` (constant-time comparison); CSRF defense via Origin header
check (falls back to Referer); 8 KB body cap; 30-second handler timeout; HTTP/1.0
(no persistent connections); hardening headers (`Referrer-Policy`, `X-Content-Type-Options`,
`Cache-Control: no-store`).  Auto-stops after the page is served (triggered from the GET
handler, never from a POST).

**LOCAL posture caveat**: the per-invocation token gates other local processes on the same
host — it does not authenticate across a network.  A hosted deployment must add TLS,
real authentication, and a role-checking authorizer before exposing the console to
external traffic.

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
fail closed (authorized egress is refused, never faked) rather than caging.

`local_cli` nodes are now wired to the configurable egress posture (Slice 2, C-081).
OPEN is the default (subscription CLI unchanged). ALLOWLIST is opt-in via `bl graph init`,
the `BOUNDED_LOOPS_EGRESS_POSTURE` env var, or `~/.bounded-loops/egress.json`.
Fail-closed rule: ALLOWLIST without the Seatbelt cage present raises `GraphValidationError`
at preflight — it never silently falls back to OPEN.

**Caveat**: ALLOWLIST is a network-only cage. A compromised subprocess retains full read/write
access to the filesystem (HOME, TMPDIR, workdir). It cannot make outbound TCP connections
except via the loopback proxy, but it can still read or write local files.

What remains deferred is making ALLOWLIST the **default** tier for connector nodes —
today `open` remains the default. The cage and opt-in wiring are shipped.

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

---

## Runtime dependencies — why pytest ships with the package

`pip install bounded-loops` installs `pytest>=8.0` as a core runtime dependency.
This is intentional, not a packaging error.

The engine ships a built-in `pytest` gate kind. When a bounded loop declares
`gate.kind: pytest` (in `loop.yaml`), the engine invokes `python -m pytest` as a
subprocess at run time — not as a test framework for this project's own test suite,
but as the independent gate that checks the workspace. Because that subprocess call
is part of the engine's runtime path (not just a development or CI tool), pytest
must be present in the same environment as the engine itself.

Note: `kind: loop` as a **graph node kind** is distinct from a standalone loop using
a pytest gate. `kind: loop` graph nodes are not executable via `bl graph run --execute`
in this release — preflight refuses them. See the capability matrix above.

A test (`test_default_install_includes_pytest_for_shipped_pytest_gates`) asserts this
dependency is present and reachable, specifically to prevent well-meaning packaging
cleanup from moving pytest to a dev-only optional group and silently breaking the
pytest gate for users who install only `bounded-loops`.

If you install `bounded-loops` and never use the pytest gate, pytest is an unused
runtime dependency in your environment. That is the accepted tradeoff.
