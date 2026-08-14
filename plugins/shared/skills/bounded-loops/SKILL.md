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

## Loop or graph?

**A loop is one task driven to a verified finish.** A worker attempts it, an independent gate
decides, and it retries to a hard attempt bound. **A graph is a DAG of loops** with dataflow
between them, plus the node kinds a loop cannot express: a human approval, a join over branches,
a publish with a policy.

Pick a **loop** when there is **one checkable outcome**. "Make the test suite pass", "get this
file to validate against the schema", "remove every secret this scanner finds" — one condition,
one mechanical check, done.

Pick a **graph** when either of these is true:

- **Several outcomes have to hold together**, and a partial result is not a result. Three loops
  that each pass individually still tell you nothing about whether the combination is safe to
  ship; a join node is the thing that asks that question.
- **Something irreversible happens at the end** — a publish, a payment, an external write. Then
  you want an approval node in front of it, declared effects on it, and a receipt proving what
  was authorized.

What the graph costs you, honestly: a manifest to author, a compile step that can refuse it, a
plan, a workspace per node, and a run directory. For a single checkable task that overhead buys
nothing. Reach for a graph when you need **causality between verified steps**, not when you want
several things done.

Rule of thumb: if you cannot name the join or the irreversible effect, you wanted a loop.

## Writing a gate that is not "an LLM says it's fine"

This is the part that gets written wrong by default, and it is the part everything else rests on.
A gate has two requirements, and a gate failing either one is decoration.

**1. Independence — the gate must be a different object from the producer.** Not a different
prompt. Not the same model with a review instruction. A *different object*, whose verdict the
worker cannot author. If the worker's output text can become the gate's verdict by any path, the
worker decides its own completion and the engine's one invariant is gone.

**2. Mechanicality — the verdict must come from observable state, deterministically.** A process
exit code. A parsed report. A schema validation result. A diff that is empty or is not. Something
you could check yourself, twice, and get the same answer.

Gates that FAIL the test, all of which look reasonable when you write them:

| Looks like a gate | Why it isn't |
|---|---|
| "Ask the model to review its own output and confirm it's correct" | Same object. The worker satisfies it by rewording. |
| "Ask a second model whether the first model did a good job" | Different object, but not mechanical — no observable state, and its verdict is prose. |
| "Check the output contains the word DONE" | Mechanical, but the worker controls the output. It will write DONE. |
| "Run the tests the agent just wrote" | The producer authored the check. Pin the test suite, or gate on tests it cannot edit. |
| "Confirm the file was modified" | Measures activity, not correctness. Touching the file passes. |

Gates that pass the test: `pytest` and its exit code. `jsonschema` against a schema the worker
cannot write. `gitleaks` finding zero secrets. A build that compiles. A checksum matching. A
reconciliation that balances.

The question to ask of any gate you propose: **could the worker satisfy this by changing what it
says, rather than by changing what is true?** If yes, it is not a gate.

Use `bl_capabilities` for the gate kinds this deployment actually has, and note the
`available_here` flag — a gate whose binary is not installed cannot verify anything, and the
engine will refuse the graph rather than pretend.

## `attempt` alone is not an identity

Once repair exists, this bites, and it bites silently.

A node retries up to `max_attempts`. Separately, `on_failure: {mode: repair, target: <ancestor>}`
sends the run **backwards** to an ancestor node — that boundary is a **repair round**. And
**attempts reset at a repair boundary.** So `(node_id, attempt=2)` names two different pieces of
work: the second attempt of round 0, and the second attempt of round 1.

Anything keyed per-try must carry **`(attempt, repair_round)`**. That includes idempotency keys,
per-attempt artifact paths, approval coordinates, and any cache you add. A receipt keyed on
`(node_id, attempt)` alone is ambiguous the moment a repair round happens, and the failure is not
an error — it is a *collision*, where round 1's work silently reuses round 0's key. This is why
`NodeWorkerPort.execute` and `IndependentGatePort.evaluate` both take `repair_round` as a
required keyword argument: it cannot be forgotten.

Termination is bounded, and you can compute the bound: at most
**`(1 + repair_budget) × Σ max_attempts` over all nodes**. `repair_budget` is a *global* bound on
rounds, not per node — which is why a graph declaring `on_failure: repair` without a
`repair_budget` above 0 is refused outright. There is no configuration in which repair runs
forever.

One consequence worth stating: a human approval granted in round 0 does **not** authorize round 1.
The work is different work.

## Reading a receipt, and why a non-DONE status is never success

**Report the status the engine returned, verbatim.** Not a summary of it, not your reading of
what "mostly" happened. The whole reason this tool exists is that an agent's own account of its
work is not evidence — and that applies to you reporting on a run exactly as much as it applies
to the worker inside one.

Loop-level terminal statuses:

| Status | What it means | Is it success? |
|---|---|---|
| `DONE` | The gate passed **and** any required approval was granted | **Yes — this one only** |
| `HALT` | A safety bound tripped: budget, attempt cap, or no progress | No |
| `PAUSE` | The gate passed but an approval is required and not yet granted | No — it is waiting for a human |
| `KILLED` | An external kill switch tripped between laps | No |
| `ERROR` | The runner or gate failed before a verdict could complete | No — there is no verdict at all |

Run-level states are `SUCCEEDED`, `FAILED`, `CANCELLED`, plus `HALTED` and `EXPIRED`. Only
`SUCCEEDED` is success.

None of the non-success statuses is partial success:

- `HALT` / `HALTED` means the work stopped because a bound said stop. Some nodes may have
  succeeded. **The run did not.** "3 of 4 nodes passed" is a fact about nodes, not a result.
- `ERROR` is the one most often misreported, because something clearly happened and there is
  output to summarise. But an `ERROR` run has **no verdict** — the gate never returned one. There
  is nothing to be optimistic about, because nothing was checked.
- `PAUSE` is not a failure and not a finish. It is a question addressed to a human. The correct
  response is to surface the approval, not to work around it.

What to read, in order:

1. **The status.** Then say it.
2. **The gate's verdict and reason** — which gate ran, what it observed, and why it decided as it
   did. This is the evidence; the rest is context.
3. **Which package digest actually ran**, for a loop node. A digest that is not the one you
   expected means the thing you reviewed is not the thing that executed.
4. **The controls the sandbox actually enforced** — not the tier that was requested. The engine
   publishes an honest list, and it is shorter than the tier name suggests on some hosts.
5. **Spend**, against the ceiling. A run that stopped on its budget is a `HALT`, and resuming it
   without raising the ceiling asks it to stop again.

Two things that are audit metadata and never completion signals: `agent_claimed_done`, and any
text in the worker's output that reads like a conclusion. If the gate did not pass, the worker
saying it finished is a record of a claim, not a result.

If a run is not `DONE` and the user asked you to complete the task, the task is not complete.
Say which status came back, say what the gate objected to, and either fix that or ask. Do not
close the loop yourself — that is the one thing this engine exists to prevent.

---

## Capability matrix

All claims in this section are verified against source.
For the machine-readable version, call `bl_capabilities` (MCP) or
`bl capabilities` (CLI, add `--refusals` for the refusal table alone).

### Node kinds (10 total)

| Kind | Kind-specific required fields | Notes |
|---|---|---|
| `loop` | `loop_package` (sha256 digest, `sha256:<64 hex chars>`) | Source: `authoring.py:66`, schema:179. Digest is CONTENT-ADDRESSED — never invent one; get a real one from `bl_search_loops` / `bl_catalog`, which report the digest of every shipped package. |
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
| `bl_run(loop_dir=..., confirm=false)` | Preview: show runner, gate, command — do not execute. Returns a `confirm_token` |
| `bl_run(loop_dir=..., confirm=true, confirm_token=...)` | Execute. The token from the preview is required |

### Graph tools

| Tool | What it does |
|---|---|
| `bl_search_loops(task_description=...)` | Rank the shipped loop catalog against a task. LEXICAL, not semantic — candidates to read, never a decision |
| `graph_plan(manifest_yaml=...)` | Compile a manifest to an execution plan — nodes, edges, ceilings, where it pauses |
| `graph_status(run=...)` | Show the current state of a graph run from its receipts |
| `graph_approve(run=..., node_id=..., decision=...)` | Record a human decision on a paused node. `confirm=false` previews |
| `graph_metrics(run=...)` | What the independent gate actually achieved on a run, with spend per node |

The server refuses `bl_run(confirm=true)` without the `confirm_token` that the
preview returned. This is a safety feature, not a bug. The token is signed over
the exact run you previewed — gate command, runner, iteration cap, and a hash of
the loop's files — so editing `loop.yaml` between preview and confirm invalidates
it. Preview again; do not try to work around it.

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
    loop_package: "sha256:<from bl_catalog — never invented>"
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
    loop_package: "sha256:<from bl_catalog — never invented>"
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
bl loops list --keyless        # every shipped package, with its content digest
# or, from a host: bl_search_loops("red green refactor")
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
