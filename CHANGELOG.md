# Changelog

All notable changes to bounded-loops are documented here. This project follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- **Gate false-accept rate intervals now report measured coverage instead of an assumed one.**
  The Wilson score interval required independent Bernoulli trials, which retried
  attempts violate; its coverage under correlated retries measured 31–41%. It is
  replaced by an empirical-Bernstein interval with a predictable plug-in, whose
  coverage under the same simulated regime measured 96.9%. The `bl graph metrics`
  label changes from `nominal-95% iid (UNCALIBRATED)` to
  `emp-Bernstein 95% (COVERAGE-MEASURED)`.

  **This is NOT an anytime-valid confidence sequence.** The radius is the
  fixed-time empirical-Bernstein form and carries no stitching term, so
  simultaneous validity over all sample sizes does not follow from it. The
  measured figure is also coverage of the per-run latent rate, a different
  estimand from the marginal false-accept rate — do not quote it as α coverage.

### Fixed

- **Conditional edges (`when`) now actually apply.** An edge's `when` was accepted,
  validated and stored — then ignored by the scheduler, so a graph with a condition on
  an edge ran that edge unconditionally and nothing warned you. Conditions are now
  enforced.

  **Breaking:** `when` accepts only the source node's outcome — `succeeded`, `failed`,
  `skipped`, or `terminal` (or `null` for the default, `succeeded`). Anything else is now
  refused when the graph is validated instead of being silently dropped. If a graph of
  yours stops compiling, that condition was never being applied — the error tells you
  which edge and what the accepted values are.

  Data-dependent conditions such as `result.status == 'failed'` are not supported.

- **A condition that could never fire is refused too.** `when: failed`, `skipped`, and
  `terminal` are rejected under `fail_mode: fail_closed`, because that mode stops the run at
  the first node failure — so such an edge could never apply. The error names the mode to use
  instead. Same rule that already applies to `on_failure: continue|repair|await_human`.
- **`fail_mode: continue_declared` now does something.** It was accepted by the schema and
  ignored by the runtime, so every run was fail-closed whatever the graph declared.

### Added

- **Route around a failed node.** With `fail_mode: continue_declared`, `when: failed` runs a
  downstream node only when its upstream failed — a cleanup, notification, or fallback
  branch. `when: terminal` runs a branch whatever the outcome.

  Continuation is deliberately narrow: the run keeps going only past the node's own
  bounded-loop outcome (gate rejection, worker fault, unverified artifact, spent budget,
  exhausted re-drives). A broken gate, a denied policy or isolation refusal, a missing
  worker, a rejected or unresolved approval, an exhausted spend cap, a broken worker
  contract, or an unmeasurable budget still stop the run — continuing past those would keep
  spending, trust an unreliable gate, or route around a control.
- **Untaken branches are recorded, not stranded.** A node whose every incoming condition
  excluded it is marked SKIPPED, with the reason on its receipt, and a run whose only
  unfinished work was an untaken branch completes successfully instead of reporting a
  failure. A node that failed still fails the run.
- **Repair a node upstream (`on_failure: repair`).** When a node exhausts its retry budget it
  can send the run back to an ancestor, which then re-runs along with everything downstream of
  it. Write it as `on_failure: {mode: repair, target: <node_id>}` and set a
  `policies.repair_budget`.

  The budget is a **global** cap on repair rounds for the whole run, not per node, and that is
  what makes the run provably finish: total node executions are bounded by
  `(1 + repair_budget) × Σ(max_attempts)`. Per-node retry budgets alone do not bound a
  graph that can repair.

  Every round is recorded — `run.repair.round` for the boundary, `node.repaired` for each node
  reset, and the round number on every receipt in it — so a run that repaired is still fully
  auditable, and a replay refuses a boundary it cannot prove legal.

  Refused up front: a target that is not a strict ancestor, a missing target, a budget of 0, or
  a halting fail mode where a repair could never begin.
- **A run's fail mode is durable.** Recorded in `run-meta.json`, so `resume` and `approve`
  drive the graph the way the original run did.

## [0.4.0] — 2026-08-12

The headline of this line is **the bounded-loops graph engine** (`bl graph`): a
DAG of independently-gated bounded loops built on the same keyless loop engine.

### Added

- **Graph engine (`bl graph`)** — compile and run a DAG of bounded loops where an
  independent gate decides each node and a producer never grades its own work.
  Subcommands: `init`, `lint`, `plan`, `run` (with `--execute`), `approve`,
  `console`, `arena`, `status`, `artifacts`, `demo`, and `studio`.
- **Guided setup (`bl graph init`)** — an interactive installer that writes your
  connector mode and egress posture to `~/.bounded-loops/egress.json`, so nothing
  has to be configured by hand. Every prompt also has a flag for scripted use.
  Defaults to running your own logged-in CLI with the network open. Credentials
  are never written to disk.
- **Approve a paused run from the CLI (`bl graph approve`)** — a run that reaches a
  human-approval checkpoint now pauses durably and exits **3** (distinct from
  success and failure) instead of being refused. Record the decision with
  `bl graph approve --run <dir> --node <id> --decision approved|rejected` and the
  run continues past the gate.
- **Approve from a browser (`bl graph console`)** — a local click-to-approve page
  for a paused run, bound to `127.0.0.1` and gated by a one-time token printed on
  start. It records decisions through the same durable path as the CLI. Intended
  for a single operator on their own machine; a shared deployment needs real
  authentication in front of it.
- **Choose how much network your connector gets** — three egress postures,
  selectable per deployment via `bl graph init`, `BOUNDED_LOOPS_EGRESS_POSTURE`, or
  the config file. `open` (**the default**) leaves your subscription CLI exactly as
  it is today, with the network open. `allowlist` is an opt-in lockdown that runs
  it inside a real macOS Seatbelt cage and permits outbound traffic only to hosts
  you list — it refuses to start rather than quietly running unconfined on a
  machine that cannot enforce it. `broker` routes API-key traffic through the
  no-secret broker.
- **Two credential-safe connector modes** — Local-CLI (runs your already-logged-in
  `claude`/`codex`/`grok`/`muse`/`agy` subscription; credentials are never read or
  logged) and BYOK/HTTPS (a frontier-model API through a no-secret egress broker
  with single-use, time-bound leases and SSRF/DNS-rebind protection).
- **Receipt-derived read-only Arena** — an append-only, hash-chained event log plus
  content-addressed artifacts, rendered as a non-executing HTML projection
  (`bl graph arena`). Local runs are marked `LOCAL/UNVERIFIED`.
- **Cross-model audit coverage** — `--audit-plan` runs independent auditor nodes;
  the Arena shows a release verdict that blocks on producer-only cells or
  unresolved high-severity findings.
- **Durable human approvals** — a decision survives a restart: it is persisted and
  rehydrated on resume, whether it was recorded from the CLI, the local console, or
  programmatically.
- **MCP graph surface** — the `bl graph` tools are exposed over MCP with
  session-bound subject identity.

### Notes

- `bl graph` is a beta. See the honest capability matrix in the README and
  [`docs/RELEASE-READINESS.md`](docs/RELEASE-READINESS.md) for exactly what is
  enforced, and where.
- Upgrading from 0.3.x needs no action: the default egress posture leaves existing
  behavior unchanged, and there is no config file to create unless you want the
  lockdown tier.
- The base loop engine, the nine bounds, the 68-loop catalog, and all `bl run`
  behavior are unchanged.

## [0.3.1] — 2026-07-13

### Fixed

- Added the standard `bl --version` probe so Python and npm clean-install
  verification can report the exact engine release.

## [0.3.0] — 2026-07-13

Minor release for the verified install, convergence, and agent-integration
experience.

### Added

- A three-lap `convergence-demo` plus a max-iteration trip variant, both
  keyless and covered by ledger assertions.
- Native Codex and Claude Code plugin manifests, a repository Codex
  marketplace, tested installation instructions, and an MCP stdio smoke test.
- A real Codex-backed citation run receipt with a machine-readable ledger and
  redacted transcript excerpt.
- `bl doctor`, `bl runs <loop> --show <run-id>`, and `bl lint --contrib`.
- Clean-room CI across macOS and Ubuntu on Python 3.11–3.13, built from the
  wheel and exercising the README, scaffolding, and MCP server.
- Reproducible terminal GIF and 1280×640 GitHub social-preview assets.

### Changed

- `pytest` is now a core dependency because shipped pytest gates invoke it.
- Codex runner failures now become auditable engine errors, live token usage is
  recorded, and non-Git scratch workspaces use Codex's explicit skip-check flag.
- The citation example now takes two deterministic laps; framework examples
  fail with exact dependency-install guidance.
- README and release metadata now use the canonical count: 68 loop folders, 64
  keyless out of the box. The README puts the verified quick start first and
  uses the real CI badge.
- The npm launcher pins the Python engine to the same version as the npm
  package, preventing silent cross-ecosystem version drift.

### Fixed

- Clean wheel installs can execute shipped pytest gates.
- Runner overrides are shown accurately in the pre-run trust preview.
- Stale CLI output examples and orphaned private-course section references were
  removed.

## [0.2.1] — 2026-07-08

Patch release for the public install experience.

### Changed
- Clarified PyPI and npm install docs: installed users start with `bl new --list`
  and scaffold a local loop; source checkouts use `bl list` for the full catalog.
- Updated public loop-count wording to distinguish 67 loop folders from the 63
  keyless, zero-setup loops.

### Fixed
- `bl list` outside a source checkout now gives actionable scaffold/clone
  guidance instead of a dead-end `No loops found.` message.
- Clean dev type-checking now passes for the full source and test tree.

## [0.2.0] — 2026-07-07

Production-hardening release. The engine moves from a runnable reference library
to a harness you can rely on in CI, while keeping the keyless-first defaults.

### Added
- **Composite gates** (`gate.kind: composite`, `mode: all`) — a loop can require
  several independent checks to pass together, with a per-child verdict recorded
  in the ledger.
- **Typed external gates**: `gitleaks`, `semgrep`, `trivy`, `promptfoo`,
  `great_expectations`, and `axe` — adapters that parse structured tool output,
  not just exit codes.
- **`Status.ERROR`** — runner/gate execution failures are now a first-class,
  auditable terminal outcome with a ledger entry, instead of an unstructured exit.
- **`docker` and `worktree` runners** for stronger, opt-in sandbox isolation.
- **Resumable runs** — `bl run <loop> --run-id <id>` persists a workspace and
  per-run ledger (indexed in SQLite); `--resume` continues it; `bl runs <loop>`
  lists prior runs.
- **New CLI commands**: `bl show` (inspect runner/gate/bounds/risk/deps),
  `bl gates` (gate kinds + local availability), `bl audit-loops` (catalog
  copy-paste readiness).
- **Expanded MCP surface**: `bl_show` / `bl_gates` / `bl_audit_loops` / `bl_runs`
  tools, catalog/manifest/prompt resources, and `run_loop` / `write_loop` /
  `audit_loop` prompts.
- **Editor adoption**: VS Code / GitHub Copilot files (`.vscode/mcp.json`,
  `.github/` instructions and prompts) and an `AGENTS.md`.
- **CI** matrix on Python 3.11–3.13, with optional gate/runner end-to-end jobs.
- **`bounds.production.yaml`** for L2/L3 loops, so keyless demos stay approval-free
  while copy-paste production use defaults to requiring human approval.

### Changed
- Loop catalog now spans all seven agentic patterns (`prompt-chaining`, `routing`,
  `parallelization`, `orchestrator-workers`, `evaluator-optimizer`,
  `augmented-llm`, `agents`), reclassified from a single pattern.
- Scratch workspaces are cleaned up after a run by default; `--keep-workspace`
  retains them for debugging.
- Runner timeouts derive from the remaining wall-clock budget.

### Fixed
- Loop integration tests no longer assume a `.venv/bin/bl` path; they invoke the
  package entrypoint directly.
- Optional OpenTelemetry tests skip correctly when only `opentelemetry-api` is
  installed.
- Removed machine-specific absolute paths from example docs; added a lint that
  fails on them.

## [0.1.0] — 2026-07-06

Initial public release: the bounded-loops engine, the nine bounds + kill switch,
67 runnable loop folders across a dozen industries, MCP server, and agent plugins.
