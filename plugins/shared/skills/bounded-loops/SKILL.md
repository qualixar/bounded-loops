---
name: bounded-loops
description: >
  Run bounded AI-agent loops and compose graph pipelines verified by
  independent mechanical gates. Use when the user asks to run a bounded loop,
  compose a graph of loops, inspect receipts, or verify task completion
  against an independent gate.
---

# bounded-loops

The engine guarantees one invariant: **the agent never decides its own completion.**
A loop is complete only when an independent gate passes. A graph run is complete
only when every node reaches a terminal state. `agent_claimed_done` is audit
metadata, never a completion signal.

<!-- JUDGEMENT: loop-vs-graph -->
<!--
  PLACEHOLDER for the author.
  Topic: loop vs graph — when to pick which.
  Include: the structural difference, the cost of the graph surface (compile,
  plan, workspace), and the rule of thumb for when the overhead is worth it.
-->

<!-- JUDGEMENT: gate-discipline -->
<!--
  PLACEHOLDER for the author.
  Topic: how to write a gate that is NOT "an LLM says it's fine".
  Include: the independence requirement (different object from the producer),
  the mechanicality requirement (deterministic verdict from observable state),
  and examples of gates that fail the test (e.g., a prompt asking the model
  to review its own output).
-->

<!-- JUDGEMENT: attempt-identity -->
<!--
  PLACEHOLDER for the author.
  Topic: why `attempt` alone is not an identity once repair exists.
  Include: the suffix-locality rule for repair, the termination bound formula
  (1 + repair_budget) * Σ max_attempts_v, and why a receipt keyed on
  (node_id, attempt) becomes ambiguous after a repair round resets the
  attempt counter.
-->

<!-- JUDGEMENT: receipt-reading -->
<!--
  PLACEHOLDER for the author.
  Topic: how to read a receipt and why a non-DONE status is never success.
  Include: what the five run-level terminal states mean (SUCCEEDED, FAILED,
  HALTED, CANCELLED, EXPIRED), that HALTED/FAILED/CANCELLED/EXPIRED are not
  partial success, and the rule that the host must report the exact returned
  status verbatim.
-->

---

## Capability matrix

All claims in this section are verified against source.
For the machine-readable version, call `bl_capabilities` (MCP) or
`python3 -m bounded_loops.graph.cli_graph_capabilities` (CLI).

### Node kinds (10 total)

| Kind | Kind-specific required fields | Notes |
|---|---|---|
| `loop` | `loop_package` (sha256 digest, `sha256:<64 hex chars>`) | Source: `authoring.py:66`, schema:179. Digest is CONTENT-ADDRESSED — never invent one; compute with `bl graph digest <dir>`. |
| `tool` | `tool_ref` (string) | Source: schema:199 |
| `router` | `routes` (dict, ≥1 entry); optional `default_route` | Source: schema:228-229. Must cover every branch or declare a default. |
| `join` | `mode` (`all_selected` / `all_successful` / `any_successful`) | Source: schema:248-251 |
| `approval` | `required_role` (string) | Source: schema:276 |
| `audit` | `audit_profile` (string) | Source: schema:298 |
| `research_source` | `source_policy` (string) | Source: schema:318 |
| `research_claim` | (none beyond baseNode) | Source: schema:330-338 |
| `subgraph` | `graph_package` (sha256 digest) | Source: schema:351 |
| `publish` | `publication_policy` (string) | Source: schema:375 |

All nodes require: `id`, `kind`, `inputs`, `outputs`, `budget`, `effects`, `isolation`.
Source: `authoring-graph.schema.json:83-92`

### Isolation tiers

| Value | What is actually enforced | Source |
|---|---|---|
| `workspace_only` | File-system scope; no network or process restrictions above OS defaults | `authoring.py:59` |
| `process_restricted` | Additional OS-level process controls (Seatbelt on macOS, bubblewrap on Linux) | `authoring.py:60` |
| `container_restricted` | Requires container runtime; refuses if Docker/Bubblewrap unavailable | `authoring.py:61`, `enforcer.py:54` |
| `customer_managed_worker` | **Schema-valid but always fails closed on every platform.** `capabilities.py:108-109` returns `(None, "no admitted customer-managed worker transport is available")`. Do not author graphs that use this tier — they cannot run anywhere. | `capabilities.py:108-109` |

Effect → minimum isolation requirement (`execution_policy.py:27-33`):

| Effect | Minimum isolation |
|---|---|
| `read_only`, `workspace_write` | `workspace_only` |
| `external_write`, `financial`, `irreversible` | `container_restricted` |

### Gate kinds (17 declared, 12 with located adapter files)

All gates are deterministic and mechanical — no gate invokes a language model for its verdict.

Adapter-confirmed gates: `command`, `pytest`, `composite`, `osv`, `checkov`, `gitleaks`,
`semgrep`, `trivy`, `promptfoo`, `great_expectations`, `jsonschema`.
Declared in `VALID_GATE_KINDS` but adapter not located: `axe`, `agentassert`, `agentassay`,
`skillfortify`, `attestar`.

Run `bl_gates()` or `bl gates` to see what is available and installed on this host.

### Failure policies

| `on_failure` value | Status | What it does |
|---|---|---|
| `fail_graph` | Honoured (default) | Node failure terminates the entire run |
| `repair` (object form only) | Honoured | Re-executes named ancestor and descendants |
| `continue` | **Declared but refused** (`on_failure_unimplemented`) | Validator rejects at compile time |
| `await_human` | **Declared but refused** (`on_failure_unimplemented`) | Validator rejects at compile time |

Repair object form: `{"mode": "repair", "target": "<ancestor_node_id>"}`.
Bare string `"repair"` is refused (`validate_graph.py:354-360`).

`max_wallclock_s` enforcement: the field is required by schema (1–86400 s),
renamed to `hard_deadline_ms` at compile time (`compile_graph.py:297`), and enforced
as a subprocess deadline by both `sandboxed_worker.py:295` and `local_cli_worker.py:268`.

### Budget fields

| Field | Required | Enforced | Notes |
|---|---|---|---|
| `max_attempts` | YES | YES | 1–100; attempt counter tracked from receipts |
| `max_wallclock_s` | YES | YES | 1–86400 s; enforced as subprocess deadline |
| `max_tokens` | NO | YES | ≥1; tracked from receipts; `SPEND_EXHAUSTED` on breach |
| `max_cost_microunits` | NO | YES | ≥0; tracked from receipts; `SPEND_EXHAUSTED` on breach |

Budget unmeasurable: if a node declares a spend budget and its worker returns no usage
data, the node fails `BUDGET_UNMEASURABLE` (it is not metered as free).

### Terminal statuses

**Run-level** (the five terminal states; source: `event_log.py:32`):
`SUCCEEDED`, `FAILED`, `HALTED`, `CANCELLED`, `EXPIRED`

A run not in one of these states is still active. Do not report a non-terminal
status as done or partial success.

**Loop-engine** (inner bounded loop; source: `domain/models.py:62-76`):
`DONE` (gate passed), `HALT` (kill-switch or gate rejection), `PAUSE` (awaiting approval),
`KILLED` (external kill), `ERROR` (runner or gate failed)

Only `DONE` is success. Report the exact status verbatim — never convert
`HALT`, `PAUSE`, `KILLED`, or `ERROR` into success language.

---

## Refusal reference (quick lookup)

When the compiler rejects your manifest, look up the error code in
[plugins/shared/docs/refusal-reference.md](../../docs/refusal-reference.md)
for the plain-language cause and the fix. The 37 codes are derived from
`bounded_loops.graph.application.refusals.REFUSAL_CODES`.

Common mistakes and their codes:

- Inventing a `loop_package` digest string → `mutable_package_reference`
- Using `on_failure: continue` → `on_failure_unimplemented`
- Using `on_failure: await_human` → `on_failure_unimplemented`
- A `when: FAILED` edge under `fail_mode: fail_closed` → `edge_condition`
- `customer_managed_worker` isolation → `execution_enforcement` at runtime
- Bare string `"repair"` instead of object form → `on_failure`
- Missing `repair_budget` when using repair → `repair_budget`

---

## Tool inventory (MCP)

Available MCP tools when `bounded-loops-mcp` is connected:

### Loop tools

| Tool | What it does |
|---|---|
| `bl_list()` | Discover all loop packages in the workspace |
| `bl_lint(loop_dirs=[...])` | Validate manifests and bounds |
| `bl_show(loop_dir=...)` | Inspect runner, gate, bounds, risk tags, production readiness |
| `bl_gates()` | List gate kinds and dependency availability on this host |
| `bl_audit_loops(dirs=[...])` | Audit loop examples for production readiness |
| `bl_run(loop_dir=..., confirm=false)` | Preview: show runner, gate, command — do not execute |
| `bl_run(loop_dir=..., confirm=true)` | Execute only after a matching preview |

### Graph tools

| Tool | What it does |
|---|---|
| `bl_graph(task=...)` | Compose a graph manifest from a task description, lint it, and show the plan — stops before execution |
| `bl_graph_compile(manifest=...)` | Compile a manifest to an execution plan |
| `bl_graph_status(run_id=...)` | Show the current state of a graph run from receipts |
| `bl_graph_approve(run_id=..., node_id=...)` | Approve a node in AWAITING_APPROVAL state |
| `bl_graph_metrics(run_id=...)` | Show token spend, cost, and timing per node |

The server refuses `bl_run(confirm=true)` without a matching preview. This is a
safety feature, not a bug.

### CLI fallback

Use when MCP is unavailable:

```bash
python3 -m bounded_loops.cli list
python3 -m bounded_loops.cli show loops/<name>
python3 -m bounded_loops.cli gates
python3 -m bounded_loops.cli lint loops/<name>
python3 -m bounded_loops.cli run loops/<name> --yes

python3 -m bounded_loops.graph.cli_graph compose --task "..." --out .bounded-loops/graphs/task.yaml
python3 -m bounded_loops.graph.cli_graph compile .bounded-loops/graphs/task.yaml
python3 -m bounded_loops.graph.cli_graph run <plan_id>
python3 -m bounded_loops.graph.cli_graph status <run_id>
python3 -m bounded_loops.graph.cli_graph approve <run_id> <node_id>
python3 -m bounded_loops.graph.cli_graph metrics <run_id>
```

---

## Worked example: composing a minimal graph

Goal: given a task "fix the failing test", compose a graph with two nodes:
a repair loop and a verification loop.

**Step 1: Author the manifest.**

```yaml
api_version: bounded-loops.dev/graph/v1
graph_id: fix-and-verify
version: 1.0.0
nodes:
  - id: fix
    kind: loop
    loop_package: "sha256:<run `bl graph digest loops/bug-fix-red-green` to get this>"
    inputs: {}
    outputs:
      result: text/plain
    budget:
      max_attempts: 3
      max_wallclock_s: 300
    effects: [workspace_write]
    isolation: workspace_only
  - id: verify
    kind: loop
    loop_package: "sha256:<run `bl graph digest loops/pytest-basic` to get this>"
    inputs:
      code: text/plain
    outputs:
      outcome: application/json
    budget:
      max_attempts: 1
      max_wallclock_s: 120
    effects: [read_only]
    isolation: workspace_only
edges:
  - from_node: fix
    from_port: result
    to_node: verify
    to_port: code
connection_slots: []
policies:
  data_class: internal
  fail_mode: fail_closed
```

**Step 2: Get digests.**

```bash
bl graph digest loops/bug-fix-red-green
bl graph digest loops/pytest-basic
```

Replace the placeholder strings in the manifest. Never invent digests — they are
content-addressed and the compiler verifies them before execution.

**Step 3: Compile and inspect.**

```bash
bl graph compile fix-and-verify.yaml
bl graph compile --dry-run fix-and-verify.yaml   # shows the plan without saving it
```

**Step 4: Run.**

```bash
bl graph run <plan_id>
```

**Step 5: Read the status.**

```bash
bl graph status <run_id>
```

Report the exact status. `SUCCEEDED` = done. `FAILED` / `HALTED` / `CANCELLED` / `EXPIRED`
= not done, investigate the receipts.

---

## Gate discipline

The gate is the verifier. The runner is only a proposer.

- Prefer typed gates (`pytest`, `jsonschema`, `osv`, `checkov`, `composite`) over
  generic `command` when the output can be parsed.
- `command` gates run with `shell=False`; commands needing shell features must
  use a checked-in wrapper script.
- Missing tools, scanner crashes, empty security reports, and malformed gate
  output are not clean passes.
- `composite` gates in v1 support `mode: all`; all child gates must pass.

---

## Reporting rules

- Say `DONE` only when the loop engine returns exactly `DONE`.
- Say `SUCCEEDED` only when the graph run reaches exactly `SUCCEEDED`.
- If status is `ERROR` or `FAILED`, name whether the runner or gate failed and quote
  the short error detail.
- If status is `HALT` or `HALTED`, explain which bound tripped.
- If status is `PAUSE` or `AWAITING_APPROVAL`, explain what approval is needed.
- Include the ledger or receipt path for debugging.

---

## Agent definitions in this pack

Two agent definitions are included for use as subagents:

- **bounded-loops-composer**: given a task description, produces a validated
  graph manifest and gap tickets for any loop packages the graph needs that do
  not yet exist. Never invents digests.

- **bounded-loops-gatekeeper**: reviews a proposed gate for independence:
  checks that the checker is a different object from the producer, that the
  check is mechanical, and that a worker cannot satisfy it by rewording output.
