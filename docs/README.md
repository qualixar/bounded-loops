# bounded-loops documentation

This folder is additive documentation for **the bounded-loops graph engine**
and the base-loop engine it is built on top of. It does not replace the
top-level [README.md](../README.md), which remains the canonical quick-start
and product overview.

## The bounded-loops graph engine

- **[graph-quickstart.md](./graph-quickstart.md)** — runnable quickstart for
  `bl graph`: install, optional egress posture setup (`bl graph init`), create
  manifest/connections/inputs files, execute a Local-CLI graph (`bl graph run
  --execute`), handle approval nodes (`bl graph approve` / `bl graph console`),
  inspect with `bl graph arena` and `bl graph status`, and run the built-in
  native-sandbox demo.

- **[monitor.md](./monitor.md)** — `bl monitor`, the local web UI over a run
  directory: what it is and is not (a view, never a service), the exact security
  posture (loopback bind, per-invocation token, same-origin requirement, CSP)
  and its stated limits, how to read the panels without over-reading them, and
  starting a run from the UI.

- **[graph-capabilities.md](./graph-capabilities.md)** — honest capabilities
  and boundaries: what ships (compiler, Local-CLI and BYOK/HTTPS connectors,
  native-sandbox demo, Arena, memory spine, egress broker, cross-model audit
  engine read side, durable approvals via CLI + console + facade/MCP, egress
  posture config, MCP surface), what is wired but narrower than production
  (ALLOWLIST as the default tier, reject-path crypto gating for hosted,
  hosted receipt verifier), and what a deploying engineer must provide.

- **[graph-egress-posture.md](./graph-egress-posture.md)** — egress posture
  contract and wiring: the three postures (OPEN / ALLOWLIST / BROKER), config
  precedence (explicit arg → env var → `~/.bounded-loops/egress.json` → default
  OPEN), `bl graph init` atomic config write, macOS Seatbelt cage details, and
  the known limitation (network-only cage, not filesystem).

- **[graph-reference-composition.md](./graph-reference-composition.md)** — the
  reference wiring in `examples/graph_runtime_reference.py`: the two connector
  modes (Local-CLI and BYOK/https), `LocalGraphRuntimeFacade`, the MCP tool
  surface, and the deployment seams a client must supply.

- **[RELEASE-READINESS.md](./RELEASE-READINESS.md)** — crisp release-readiness
  assessment: what a client gets today (including `bl graph init`, `bl graph
  approve`, `bl graph console`), what requires their own configuration, the
  not-yet list, and the quality posture.

## The base-loop engine (foundation)

The graph engine composes multi-node workflows out of the same bounded,
gate-verified loop described below — one node's worker is never its own
grader, whether that node stands alone as a base loop or sits inside a graph.

- **[ARCHITECTURE.md](./ARCHITECTURE.md)** — the hexagonal (ports-and-
  adapters) design: domain (pure models + rules) vs. application (the
  `RunLoopUseCase` engine loop) vs. adapters (runners/gates/ledger/tracer)
  vs. `composition.py` (the one file allowed to wire concrete adapters in).
  Explains and diagrams the frozen invariant — the engine never trusts the
  agent's own claim of "done"; only the gate decides.

- **[NINE-BOUNDS.md](./NINE-BOUNDS.md)** — each of the nine bounds plus the
  kill switch: the exact `bounds.yaml` field, the exact engine component
  that enforces it, and why it matters. Includes a diagram of where each
  bound sits across manifest/composition/engine layers.

- **[WRITING-A-LOOP.md](./WRITING-A-LOOP.md)** — a concrete how-to: the
  nine-file scaffold `bl new` produces, the three keyless gate patterns
  (jsonschema / command+stdlib-checker / pytest), how the stub cassette
  replays a recorded fix, the `forbid:` anti-tamper guard, the L1/L2/L3
  rung ladder, and the verify protocol (`bl lint` + `bl run --yes`) —
  worked through against the real `loops/citation-existence-check/` loop.
