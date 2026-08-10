# bounded-loops documentation

This folder is additive documentation for the engine's internal
architecture — it does not replace the top-level [README.md](../README.md),
which remains the canonical quick-start and product overview.

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

- **[graph-quickstart.md](./graph-quickstart.md)** — runnable quickstart for
  `bl graph`: install, create manifest/connections/inputs files, execute a
  Local-CLI graph (`bl graph run --execute`), inspect with `bl graph arena`
  and `bl graph status`, and run the built-in native-sandbox demo.

- **[graph-capabilities.md](./graph-capabilities.md)** — honest capabilities
  and boundaries: what ships (compiler, Local-CLI connector, native-sandbox
  demo, Arena, memory spine, egress broker, cross-model audit engine, MCP
  surface), what is an implemented seam not yet wired as a run mode (BYOK/HTTP
  connector, enterprise egress firewall, hosted receipt verifier), and what a
  deploying engineer must provide.

- **[RELEASE-READINESS.md](./RELEASE-READINESS.md)** — crisp release-readiness
  assessment: what a client gets today, what requires their own configuration,
  the not-yet list, and the quality posture.
