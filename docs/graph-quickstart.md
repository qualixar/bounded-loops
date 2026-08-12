# Graph Quickstart

A graph is a portable, compiler-validated description of a multi-node agent workflow.
The engine compiles a manifest against admitted connections, runs each node inside a
native OS sandbox (macOS Seatbelt or Linux bubblewrap — no Docker), and writes a
hash-chained, receipt-backed run directory that `bl graph status` and `bl graph arena`
can read and render.

This guide walks a single-node `research_claim` graph wired to the `claude` CLI.
The same shape works for `codex`, `grok`, `muse`, and `agy` — swap `provider_id`.

---

## 1. Install

```bash
pip install bounded-loops
```

Verify:

```bash
bl graph --help
```

---

## 2. Configure egress posture (optional)

By default the engine runs connector nodes with `OPEN` egress — the subprocess can
reach any host.  To opt into the macOS Seatbelt ALLOWLIST cage (network-only; a
compromised subprocess still has full filesystem access):

```bash
bl graph init --posture allowlist --allowlist api.anthropic.com
```

`bl graph init` writes `~/.bounded-loops/egress.json` atomically (temp file +
`fchmod 0600` + `fsync` + round-trip verify + `os.replace`).  Pass `--yes` to skip
the interactive prompts.  Skip this step entirely to keep the default OPEN posture.

---

## 3. Prerequisites

The Local-CLI connector runs **your already-logged-in agent CLI** as a subprocess.
The engine never reads, stores, or logs your credentials. Authentication happens
out-of-band in the CLI's own config (subscription / print mode).

For this example, `claude` must be installed and logged in:

```bash
which claude   # must resolve
claude -p "echo test" >/dev/null   # must exit 0
```

The supported provider IDs and their subscription-mode invocations are:

| `provider_id` | CLI binary | How the prompt is delivered |
|---|---|---|
| `claude` | `claude -p` | stdin |
| `codex` | `codex exec --skip-git-repo-check` | positional argument |
| `grok` | `grok -p` | positional argument |
| `muse` | `muse exec` | positional argument |
| `agy` | `agy -p` | positional argument |

---

## 4. Create the three input files

These files come directly from the engine's own test suite. Copy them verbatim
and they will pass the compiler.

**`manifest.yaml`** — the portable graph (no credentials, no absolute paths):

```yaml
api_version: "bounded-loops.dev/graph/v1"
graph_id: agent-run
version: "1.0.0"
nodes:
  - id: agent
    kind: research_claim
    inputs: {}
    outputs: {claim: text}
    budget: {max_attempts: 1, max_wallclock_s: 30}
    effects: [workspace_write]
    isolation: process_restricted
    connection_slot: model
edges: []
connection_slots:
  - id: model
    requires: [text_generation]
    data_class_max: public
policies:
  data_class: public
  fail_mode: fail_closed
```

**`connections.json`** — the admitted connection for the `model` slot.
The object must contain exactly these 17 keys — the compiler rejects any extra or
missing field. The `admission_digest` and `route_policy_digest` are sha256 strings
that your admission workflow produces; the values below are valid stand-ins for a
local run:

```json
[
  {
    "binding_id": "binding-1",
    "slot_id": "model",
    "connector_id": "local-cli",
    "connector_version": "1.0.0",
    "connection_id": "conn-1",
    "admission_digest": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "route_policy_digest": "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
    "provider_id": "claude",
    "model_target": "subscription",
    "region": "local",
    "fallback": false,
    "capabilities": ["text_generation"],
    "data_class_max": "public",
    "allowed_effects": ["workspace_write"],
    "isolation": "process_restricted",
    "transport": "local_cli",
    "admitted": true
  }
]
```

Connection constraints the compiler enforces at compile time:
- `capabilities` must satisfy every `requires` in the slot (`text_generation` here).
- `data_class_max` rank must be >= the graph's `data_class` (`public` >= `public`).
- `allowed_effects` must include every effect the node declares (`workspace_write`).
- `isolation` rank must be >= the node's `isolation` (`process_restricted` >= `process_restricted`).
- `admitted` must be `true`.

**`inputs.json`** — run-time prompts, one per node ID. Prompts are never baked into
the portable graph; they are supplied here at execute time:

```json
{"agent": "Summarize the current AI Agent Reliability Engineering landscape in 200 words."}
```

---

## 5. Run the graph

```bash
bl graph run --execute manifest.yaml \
  --connections connections.json \
  --inputs inputs.json \
  --out ./my-run
```

The engine:
1. Compiles the manifest against `connections.json` (compile errors exit early with a clear message).
2. Runs a preflight check — refuses any node whose binding is neither `local_cli`
   nor `https` (with a matching admitted connection).  Approval nodes are **not**
   refused; they are skipped at preflight and the run pauses when they are reached.
3. Probes the platform for native sandbox support (macOS Seatbelt / Linux bubblewrap).
4. Runs each admitted `local_cli` node: invokes `claude -p`, pipes the prompt to stdin, captures stdout as the content-addressed output artifact.
5. Gates each node with an independent structural acceptance gate.
6. Writes a hash-chained run directory to `./my-run`.

Exit codes:
- `0` — SUCCEEDED (all nodes passed their gates)
- `2` — compile error, preflight refusal, or node failure
- `3` — AWAITING_APPROVAL (the run has paused at an approval node; use `bl graph approve` to record a decision and resume)

Sample terminal output on success:

```
Local-CLI graph run — REAL execution (your own subscription agent CLI)
==============================================================
run_state : SUCCEEDED
  OK node 'agent': SUCCEEDED  artifact=sha256:a1b2c3d4e5f6...
out       : ./my-run

Open the visual Arena:  bl graph arena --run ./my-run
```

The prompt is intentionally not persisted in the run directory. The run directory
contains `manifest.yaml`, `connections.json`, `plan.json`, `run-meta.json`, and
`controller-events.jsonl` (the append-only event log). The artifact (the node's
reply) is content-addressed in `./my-run/artifacts/`.

---

## 6. Handle approval nodes (exit code 3)

If your graph contains an `approval` node and the run pauses there, `bl graph run`
exits with code 3 and prints a hint such as:

```
run_state : AWAITING_APPROVAL
  PAUSED node 'review': waiting for human decision
  Resume:  bl graph approve --run ./my-run --node review --decision approved
```

To record a decision and resume:

```bash
bl graph approve --run ./my-run --node review --decision approved
# or: --decision rejected
```

`bl graph approve` exits 0 if the run has now SUCCEEDED, 2 if it FAILED, and 3
if it is still AWAITING_APPROVAL (more approval nodes remain).

**Click-to-approve in a browser:** If you prefer not to type the CLI command,
start the loopback console in another terminal while the run is paused:

```bash
bl graph console --run ./my-run
# Prints: http://127.0.0.1:<port>/  token=<token>
# Open the printed URL in any browser on this host.
```

The console is local-only (127.0.0.1, never 0.0.0.0), per-invocation token,
and auto-closes after the page is served.

---

## 7. Inspect the run

**Arena — a self-contained, read-only HTML page:**

```bash
bl graph arena --run ./my-run
# Writes ./my-run/arena.html by default.
open ./my-run/arena.html   # macOS; use xdg-open on Linux
```

**Status — a text projection of the event log:**

```bash
bl graph status --run ./my-run
```

Output includes a `LOCAL/UNVERIFIED` notice. The projection is read from the local
event log; it has not been verified against a remote Arena server. This is the
expected posture for a local run.

---

## 8. Lint and plan (authoring workflow)

Before running, validate the manifest in isolation:

```bash
bl graph lint manifest.yaml
```

Compile against connections to check binding resolution and policy constraints:

```bash
bl graph plan manifest.yaml --connections connections.json
```

Both commands accept `--json` for machine-readable output.

---

## 9. Run the built-in native-sandbox demo

To prove native OS sandbox isolation without any agent CLI or credentials:

```bash
bl graph run --execute --out ./sandbox-demo
```

No manifest is required. The engine runs a built-in probe node inside macOS
Seatbelt or Linux bubblewrap, attempts network access and out-of-workspace writes,
and gates on whether the OS actually denied them. The independent gate — not the
node itself — decides SUCCEEDED or FAILED.

---

## 10. In-process demonstration (no sandbox, no execution)

```bash
bl graph demo --out ./demo-run
```

This is a DEMONSTRATION. A prominent banner in the output and in `run-meta.json`
marks it explicitly: nodes are **not** executed in a sandbox; no isolation,
network, or E2 enforcement applies. Use it to inspect the run-directory structure
and exercise `bl graph status` / `bl graph arena` without any agent CLI installed.

---

## 11. Visual authoring

```bash
bl graph studio --out ./graph-studio.html
open ./graph-studio.html
```

Opens a self-contained HTML authoring tool. Pass `--from manifest.yaml` to load an
existing graph for editing (the manifest is validated before loading).

---

## Common errors

| Error | Cause | Fix |
|---|---|---|
| `compile failed [no_admitted_connection]` | No connection in `connections.json` satisfies the node's slot | Check that `capabilities`, `allowed_effects`, `isolation` rank, and `data_class_max` rank all match |
| `node 'agent' binds provider 'claude', which is not a known agent CLI` | `provider_id` in connections.json is not a known profile | Use one of: `claude`, `codex`, `grok`, `muse`, `agy` |
| `local-CLI node 'agent' exited 1: ...` | The agent CLI exited with an error (expired login, etc.) | Log in to the agent CLI and retry |
| `agent CLI 'claude' is not installed on this host` | `claude` not in PATH | Install the Claude CLI and verify `which claude` |
| `no run-time prompt was supplied for local-CLI node 'agent'` | `inputs.json` does not contain a key for `"agent"` | Add `"agent": "your task"` to `inputs.json` |
| `graph run --execute requires --out <dir>` | `--out` missing | Add `--out <directory>` |
