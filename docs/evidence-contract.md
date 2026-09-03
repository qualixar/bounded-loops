# The evidence contract — `bounded-loops.dev/slm-bridge/v1`

A stable, narrow document another product can read to observe what this engine did, over MCP
alone: no `import bounded_loops`, no parsing our receipt files, no pinning our package version.

SuperLocalMemory is the first consumer. It is an **optional** one. bounded-loops does not know
whether SLM exists, gains nothing from its presence, and is a complete product installed by
itself.

---

## Compatibility is the contract id, not our version

```json
"evidence_contracts": [
  {
    "id": "bounded-loops.dev/slm-bridge/v1",
    "tool": "bl_graph_evidence",
    "operation": "observe_terminal_run"
  }
]
```

`bl_capabilities` publishes this. **Branch on `id`. Never on `engine.version`.**

`engine.version` says which build produced a document — provenance, useful in a bug report,
worthless as a compatibility signal. A consumer pinned to `0.6.2` breaks on `0.6.3`. A consumer
that reads `bounded-loops.dev/slm-bridge/v1` keeps working across 0.7, 1.0 and 2.0.

Inside v1: fields may be **added**; no required field changes meaning or disappears. A `v2`
would be advertised alongside v1, not in place of it.

## V2 execution-learning companion

`bounded-loops.dev/slm-bridge/v2` is advertised alongside v1 through
`bl_capabilities`, with `bl_graph_execution_evidence` as its read-only fetch
tool. It derives a fixed-vocabulary execution-reliability eligibility signal
from the same reconciled terminal receipt. V1 remains exactly observation-only
with `eligible_for_learning: false`.

V2 never contains prompts, paths, commands, gate prose, artifact contents, or
user/assistant text. It can identify a verified gate success or a gate
rejection, but it cannot establish semantic facts, preferences, authorization,
or shared/global promotion. Consumers must validate the immutable receipt and
may rebuild or delete their derived learning independently.

## Two tools

```
bl_graph_terminal_runs(limit: int = 100) -> {run_ref, run_id, run_state, terminal_at}[]
bl_graph_evidence(run_ref: str)          -> the document below
```

Poll the first, diff against what you have already observed, fetch only what is new. Both are
read-only. Neither takes a secret.

**`run_ref` is the address; `run_id` is the identity.** They are genuinely different — a run
records its own name in its receipts and lives in a directory that may be named something else.
Pass `run_ref` back to fetch; use `run_id` when you mean the run itself.

`bl_graph_evidence` takes **`run_ref`**. Until 0.6.3 its published schema named that argument
`run_id`, so a consumer reading the schema would pass the identity and get a refusal — the
tool did the right thing under the wrong name.

## The document

```json
{
  "contract": "bounded-loops.dev/slm-bridge/v1",
  "workspace_id": "sha256:<hex>",
  "run_ref": "<safe-id>",
  "run_id": "<safe-id>",
  "organization_id": "<safe-id>",
  "project_id": "<safe-id>",
  "outcome": "SUCCEEDED | FAILED | CANCELLED",
  "run_state": "SUCCEEDED | FAILED | HALTED | CANCELLED | EXPIRED",
  "demonstration": false,
  "eligible_for_learning": false,
  "terminal_at": "2026-08-15T10:30:00Z",
  "graph_digest": "sha256:<hex>",
  "plan_digest": "sha256:<hex>",
  "policy_digest": "sha256:<hex>",
  "receipt": {
    "sequence": 42,
    "head_digest": "sha256:<hex>",
    "trust": "local_hash_chain_only"
  },
  "nodes": [
    {
      "node_id": "<safe-id>",
      "state": "<graph node state>",
      "gate_passed": true,
      "attempts": 1,
      "artifact_digests": ["sha256:<hex>"]
    }
  ]
}
```

### Fields that exist to stop a consumer being misled

**`outcome` vs `run_state`.** The engine has five terminal run states; `outcome` has three
buckets, so it is lossy by construction. `HALTED` — a budget or policy stop — and `FAILED` —
work the gate rejected — are different events. `outcome` is there for consumers that want a
simple answer; `run_state` is there so the simple answer is not the only record. **Only
`SUCCEEDED` is success**, in both fields. Nothing upgrades a non-success into a partial one.

**`demonstration`.** `true` means a cassette or stub replay. It proves the wiring works and
proves nothing whatever about the work. A consumer that cannot tell a scripted success from a
real one will eventually learn from a fixture.

**`eligible_for_learning`.** Always `false` in v1. This is the refusal in the payload rather
than only in this document, because a consumer reads JSON, not prose. **This evidence supports
observation only.** It does not authorize automatic learning, memory ranking, model routing, or
any other downstream act.

**`gate_passed` is tri-state.** `null` means no gate ran — an approval node, a join, or a node
that failed before reaching its gate. Flattening that to `false` would credit or blame a gate
for a judgement it never made.

**`attempts`.** Passing on attempt 1 and passing on attempt 5 are different evidence. A retry
engine that hides its retry count has discarded the thing that makes it a bounded loop.

### `receipt.trust` is `local_hash_chain_only`, and stays that way

The receipt log is an append-only hash chain on local disk. That makes tampering **detectable**
by anyone holding an earlier head. It is **not** authentication, notarization, or independent
audit. Do not relabel it `verified` — that would hand a consumer a guarantee no part of this
system provides.

### What never travels

Gate reasons, artifact contents, filesystem paths, executable commands, environment values,
secrets, and any free text a user or a gate wrote. `workspace_id` is a digest precisely so the
location of somebody's source tree is not a field in a message bus. Every string is
shape-validated before it leaves, and a structural sweep refuses the whole document if anything
path-shaped reaches a field.

### Refusals

Non-terminal runs are refused — a consumer that caches a mid-flight verdict has recorded
something that never happened. Unsafe run references are refused by the same validator the run
store uses, so `../` never resolves. Both come back as:

```json
{"status": "unavailable", "contract": "...", "reason": "<why>"}
```

A refusal is an ordinary answer to a poll, not a broken server.

## Producer-side layout

| File | Role |
|---|---|
| `bounded_loops/graph/application/slm_bridge.py` | contract id, shape, validation — pure |
| `bounded_loops/graph/slm_evidence.py` | run resolution and receipt reading — all the I/O |
| `bounded_loops/mcp_discovery.py` | the two MCP tools |
| `tests/graph/test_slm_bridge.py` | contract shape, refusals, leak sweep |
| `tests/graph/test_slm_evidence_end_to_end.py` | against a real run directory |
