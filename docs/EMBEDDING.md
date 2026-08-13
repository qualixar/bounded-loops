# Embedding bounded-loops

This document tells you what is public, what is internal, what semver covers, and how to
embed either the loop engine or the graph engine in your own code or service.

---

## The contract

**Everything in `bounded_loops.__all__` is public and stable.**  Semver guarantees apply:
no breaking change without a major-version bump.

Everything NOT in `__all__` — every `bounded_loops.application.*`, `bounded_loops.adapters.*`,
`bounded_loops.graph.application.*`, `bounded_loops.graph.adapters.*`, etc. — is **internal**.
Internal paths can change in any release, including patch releases, without notice.

```python
import bounded_loops

# The full public surface:
print(bounded_loops.__all__)
# ['__version__', 'load_loop', 'LoopManifest', 'wire', 'Bounds', 'Outcome', 'Status',
#  'NodeWorkerPort', 'WorkerResult', 'IndependentGatePort', 'GateVerdict']
```

### What semver covers

| Stability | Guarantee |
|-----------|-----------|
| Names in `__all__` | Stable across compatible releases. |
| `bounded_loops.__version__` | PEP 440, semver-aligned. |
| The `bounded-loops-mcp` server's tool names and payload shapes | Stable (documented below). |
| Anything not in `__all__` | Internal. No guarantee. |

---

## The recommended integration path: MCP

If your tool or agent is already an MCP client, the **MCP server is the front door**.
You get loop listing, lint, preview, and execution in a single JSON-RPC call — no Python
import required.

### Install the MCP extra

```bash
pip install "bounded-loops[mcp]"
```

### Start the server

```bash
bounded-loops-mcp
```

The server listens on stdio (standard FastMCP transport).  Point any MCP client at it.

### MCP tools (verified against the running server)

| Tool | Side-effects | What it does |
|------|-------------|--------------|
| `bl_list` | none | List all loops found under `<repo-root>/loops/` |
| `bl_lint` | none | Validate one or more loop manifests |
| `bl_show` | none | Show a loop's manifest, runner, gate, bounds, hashes |
| `bl_gates` | none | List gate kinds and local dependency availability |
| `bl_audit_loops` | none | Audit loops for copy-paste production readiness |
| `bl_run` | yes | Run a loop (requires a `confirm=false` preview first) |
| `bl_runs` | none | List persisted run metadata for a loop directory |

**`bl_run` safety model**: `confirm=false` returns a preview and records it in the
server session.  `confirm=true` executes only if the gate command, runner kind, iteration cap,
and content hash all match the preview recorded for this session.  A loop that would require
interactive approval (L2/L3 without `require_approval: false` in `bounds.yaml`) is refused
before execution — there is no interactive terminal over MCP.

### MCP resources and prompts

| Name | Kind | Purpose |
|------|------|---------|
| `bounded-loops://catalog` | resource | Loop recipe catalog (markdown) |
| `bounded-loops://loop/{name}/manifest` | resource | A loop's manifest as JSON |
| `bounded-loops://loop/{name}/prompt` | resource | A loop's PROMPT.md |
| `run_loop` | prompt | Guided: inspect → lint → preview → run |
| `write_loop` | prompt | Guided: author a production-grade loop |
| `audit_loop` | prompt | Guided: audit a loop for production readiness |

### Worked MCP example (Python MCP client)

```python
import asyncio
import json
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    server = StdioServerParameters(command="bounded-loops-mcp", args=[])
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # 1. List available loops.
            result = await session.call_tool("bl_list", {})
            loops = json.loads(result.content[0].text)["loops"]
            print("Loops:", [l["name"] for l in loops])

            # 2. Inspect the bug-fix-red-green loop.
            loop_dir = next(l["path"] for l in loops if l["name"] == "bug-fix-red-green")
            show = await session.call_tool("bl_show", {"loop_dir": loop_dir})
            print("Manifest:", show.content[0].text[:120])

            # 3. Preview (confirm=false) then run (confirm=true).
            preview = await session.call_tool(
                "bl_run", {"loop_dir": loop_dir, "confirm": False}
            )
            print("Preview:", preview.content[0].text)

            run = await session.call_tool(
                "bl_run", {"loop_dir": loop_dir, "confirm": True}
            )
            outcome = json.loads(run.content[0].text)
            print("Status:", outcome["status"])
            print("Reason:", outcome["reason"])

asyncio.run(main())
```

---

## Embedding the loop engine directly (Python API)

Use this when you want to drive a loop from inside your own Python process — e.g. in a test
harness, a CI script, or an orchestrating agent.

### Install

```bash
pip install bounded-loops
```

### Minimal example

```python
from pathlib import Path
import bounded_loops

# 1. Load a loop directory (loop.yaml + bounds.yaml) into a validated manifest.
manifest = bounded_loops.load_loop(Path("loops/bug-fix-red-green"))

# 2. Wire the manifest into a ready-to-run use case.
use_case = bounded_loops.wire(manifest)

# 3. Run.  Returns an Outcome.
outcome = use_case.run()

# 4. Inspect the result.
if outcome.status == bounded_loops.Status.DONE:
    print("Gate passed in", outcome.laps, "lap(s).")
elif outcome.status == bounded_loops.Status.HALT:
    print("Halted:", outcome.reason)
else:
    print("Terminal status:", outcome.status, outcome.reason)

print("Ledger:", outcome.ledger_path)
```

### Creating custom bounds

```python
import bounded_loops

# Override bounds at wire time by passing a manifest whose bounds you constructed.
# You do NOT need to construct Bounds manually for shipped loop.yaml files —
# load_loop() builds them from bounds.yaml.  This is for programmatic loop creation.
custom_bounds = bounded_loops.Bounds(
    max_iterations=5,
    no_progress_window=2,
    max_tokens=10_000,
    sandbox=False,              # disable sandbox for local dev
    require_approval=False,     # L1 / unattended
)
```

### Type reference

```python
bounded_loops.Outcome     # dataclass(frozen=True) — status, reason, laps, ledger_path
bounded_loops.Status      # str Enum — DONE, HALT, PAUSE, KILLED, ERROR
bounded_loops.Bounds      # dataclass(frozen=True) — the nine bounds configuration
bounded_loops.LoopManifest  # dataclass(frozen=True) — validated manifest (read-only carrier)
```

---

## Embedding the graph engine (implementing a custom node worker or gate)

The graph engine runs a DAG of independently-gated bounded loops.  An embedder plugs in
custom execution by implementing one of two Protocols:

- `NodeWorkerPort` — executes one planned node.
- `IndependentGatePort` — evaluates a worker result without re-executing the node.

### Install

```bash
pip install bounded-loops
```

### Implement a custom node worker

```python
import bounded_loops

class MyDockerWorker:
    """A custom node worker that executes nodes inside Docker."""

    def execute(
        self,
        *,
        plan,     # bounded_loops.graph.application.compile_graph.ExecutionPlan (internal)
        node,     # bounded_loops.graph.domain.plan.PlannedNode (internal)
        envelope, # bounded_loops.graph.application.execution_policy.ExecutionEnvelope (internal)
        attempt: int,
    ) -> bounded_loops.WorkerResult:
        # ... your implementation ...
        return bounded_loops.WorkerResult(
            output_artifact_digests=("sha256:abc123",),
            isolation_provider_id="docker",
            enforced_controls={"net": "blocked", "fs_write": "isolated"},
        )
```

`plan`, `node`, and `envelope` are internal types — your worker receives them from the
controller; it does not construct them.  You only need to import the public types
(`WorkerResult`, `NodeWorkerPort`) to implement and type-check your worker.

### Implement a custom gate

```python
import bounded_loops

class MySchemaGate:
    """A custom gate that validates a node's output against a JSON Schema."""

    def evaluate(
        self,
        *,
        plan,
        node,
        result: bounded_loops.WorkerResult,
    ) -> bounded_loops.GateVerdict:
        digests = result.output_artifact_digests
        passed = len(digests) > 0  # real impl: load artifact and validate
        return bounded_loops.GateVerdict(
            passed=passed,
            reason="schema valid" if passed else "no output artifact",
        )
```

### Protocol reference

```python
bounded_loops.NodeWorkerPort   # Protocol — implement execute(*, plan, node, envelope, attempt)
bounded_loops.WorkerResult     # dataclass(frozen=True) — what execute() returns
bounded_loops.IndependentGatePort  # Protocol — implement evaluate(*, plan, node, result)
bounded_loops.GateVerdict      # dataclass(frozen=True) — what evaluate() returns
```

The controller injects the concrete ports at startup.  Refer to
`docs/graph-capabilities.md` and `docs/graph-quickstart.md` for the full graph runtime
documentation including the deployment facade, connector catalog, and arena model.

---

## What is internal

Do **not** import from these paths.  They are not covered by semver and WILL change.

| Internal path | What lives there |
|---------------|-----------------|
| `bounded_loops.composition` | Wiring / composition root — internal adapters |
| `bounded_loops.application.*` | Use cases, ports, manifest internals |
| `bounded_loops.adapters.*` | Concrete runner and gate adapters |
| `bounded_loops.graph.application.*` | Graph use-case and port internals |
| `bounded_loops.graph.adapters.*` | Graph concrete adapters |
| `bounded_loops.graph.domain.*` | Graph domain models |
| `bounded_loops.graph.graph_runtime_facade` | Deployment-side facade (internal) |
| `bounded_loops.cli*` | CLI entry points |
| `bounded_loops.trust_store` | Internal trust ledger |
| `bounded_loops.hooks.*` | Internal hook dispatch |

---

## Discrepancies and known gaps

No discrepancies were found between what `mcp_server.py` advertises and what it implements:
all seven `@mcp.tool()` registrations (`bl_list`, `bl_lint`, `bl_show`, `bl_gates`,
`bl_audit_loops`, `bl_run`, `bl_runs`) match the code that runs.

**Graph MCP**: `bounded_loops/graph/mcp_graph.py` exposes `graph_status`, `graph_state_md`,
`graph_resume`, and `graph_approve` as *functions* (not FastMCP-registered tools).  These
are wired by a deployment that injects a `GraphRuntimeFacade` via `register()`.  They are
not reachable directly through the `bounded-loops-mcp` server — a deployment-specific
integration step is required to expose them.  This is by design (the graph runtime facade
is deployment-owned), but it means graph execution is not available via the default
`bounded-loops-mcp` start command without additional deployment wiring.
