<p align="center">
  <img src="https://raw.githubusercontent.com/qualixar/bounded-loops/main/assets/bounded-loops-logo.svg" alt="bounded-loops logo" width="170"/>
</p>

<h1 align="center">bounded-loops</h1>

<p align="center"><strong>Loop engineering you can actually run — every safety bound enforced in engine code, not described in a checklist.</strong></p>

<p align="center"><em>63 runnable AI-agent loops across a dozen industries — keyless, offline, and gate-verified.<br/>The runnable engine layer for the loop-engineering practice that Karpathy, Steinberger, Cherny, Osmani, and Berman defined.</em></p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache_2.0-3b82f6?style=for-the-badge" alt="License: Apache-2.0"/></a>
  <img src="https://img.shields.io/badge/tests-619_passing-22c55e?style=for-the-badge" alt="619 tests passing"/>
  <img src="https://img.shields.io/badge/runnable_loops-63-e2725b?style=for-the-badge" alt="63 runnable loops"/>
  <img src="https://img.shields.io/badge/setup-keyless_%C2%B7_offline-0a0a0a?style=for-the-badge" alt="keyless and offline"/>
  <img src="https://img.shields.io/badge/safety-9_bounds_%2B_kill_switch-ff6b35?style=for-the-badge" alt="nine bounds plus kill switch"/>
</p>

---

Clone this, and in ~30 seconds — no API key, fully offline — watch an AI-agent
loop *wreck* without its gate (it confidently claims done while the bug is still
there), then watch the same loop *stop cleanly* with the gate enforcing it. Every
loop is a real runnable folder: a **runner** (the agent) proposes a change, an
**independent gate** (pytest, a JSON-Schema check, a command, a real security
scanner) verifies it, and a small engine repeats under **nine enforced bounds +
a kill switch** until the gate passes or a bound trips.

> "Loop engineering" is a practice the community named in 2026 — *"stop prompting
> your agent; design the loop that prompts it."* This repo doesn't claim the term;
> it ships the part others describe but don't run: the **runnable, enforced
> implementation** — **63 runnable loop folders across a dozen industries**, plus a
> wider [catalog of loop recipes](catalog/README.md). Full credits below.

## Inspired by the loop-engineering community — we build on it, not against it

bounded-loops didn't invent loop engineering. It's the runnable engine layer for
a practice that brilliant people defined first, and it exists to **complement
their work, not compete with it**.

- **Andrej Karpathy** ([@karpathy](https://x.com/karpathy)) framed the "loopy era" —
  the new skill is designing loops that keep useful work moving — and named the
  exact constraint this project is built around: *you can only safely automate a
  loop when its result is easy to evaluate.* bounded-loops turns that into code:
  the **gate** is that evaluable metric, and nothing is "done" until it passes.
- **Peter Steinberger** lit the spark: *"stop prompting your agents; design the
  loops that prompt them."*
- **Boris Cherny** (Claude Code, Anthropic): *"My job is to write loops."*
- **Addy Osmani** named the practice and gave it its anatomy in his
  [*Loop Engineering*](https://addyosmani.com/blog/loop-engineering/) essay.
- **Matthew Berman's [Loop Library](https://github.com/Forward-Future/loopy)** —
  the curated catalog of loops-as-prompts that we extend into runnable, gated folders.
- **proof-loop**, **repo-task-proof-loop**, and **agentops** pioneered the runnable
  fresh-verifier loop; bounded-loops adds the full nine-bound safety envelope and
  the by-industry library on top.

What bounded-loops adds to all of it: every loop is a **real folder that runs to
`✓ [DONE]` under nine enforced bounds** — across finance, legal, healthcare,
retail, operations, enterprise/ERP, security, testing, content, research, and
business, not just software. If we've mischaracterized or missed your loop
project, [open an issue](../../issues) — this space grows by building on each other.

## Three ways to use it

1. **As a CLI** — clone, `pip install -e .`, then `bl list` / `bl run loops/<name> --yes`. The raw engine; see [Quick start](#quick-start-keyless-30s) below.
2. **As an MCP server** — the `bounded-loops-mcp` command exposes `bl_run` / `bl_lint` / `bl_list` to any MCP client (Claude Code, Cursor, Codex, Antigravity…).
3. **As an agent plugin** — [`plugins/`](plugins/) ships a ready-to-install package for **Claude Code**, **Codex**, and **Antigravity**, each with a Skill, a `/bl-run` command, the MCP wiring, and a verify-on-stop hook. Inside your agent you get **gate-verified "done" instead of the agent claiming done**. See [MCP server and IDE plugins](#mcp-server-and-ide-plugins).

## Repository layout

```
bounded-loops/
├── bounded_loops/      the engine (Python package)
│   ├── domain/           pure rules + data types, no I/O — the ports (the seam) live here
│   ├── application/      the loop algorithm (run_loop.py) + bounds enforcer + manifest loader
│   ├── adapters/         concrete runners (stub, shell, claude-code, codex, …) and
│   │                     gates (command, pytest, jsonschema, osv, checkov)
│   ├── composition.py    the ONLY file that wires adapters onto the engine (composition root)
│   ├── cli.py            the `bl` command
│   ├── mcp_server.py     the `bounded-loops-mcp` server (bl_run / bl_lint / bl_list)
│   └── hooks/            the verify-on-stop hook
├── loops/              the 67 runnable loop folders — the library (each = seed + gate + cassette)
├── catalog/            the recipe catalog — a browsable menu of loop ideas across industries
├── docs/               how the engine works — ARCHITECTURE, NINE-BOUNDS, WRITING-A-LOOP (SVG diagrams)
├── plugins/            install packages for Claude Code / Codex / Antigravity (skills, commands, hooks)
├── tests/              the 619 tests
└── README.md · CONTRIBUTING.md · LICENSE · pyproject.toml
```

`docs/` explains **how the machine works**; `catalog/` lists **which loops you can run** — different jobs, not duplicates. Every other top-level entry (`.venv/`, `__pycache__/`, the `.*_cache/` dirs) is a local build artifact that `.gitignore` already excludes from the repo.

## Install

The engine is a **Python 3.11+** package. Install it whichever way fits your stack:

**pip — native, recommended**
```bash
pip install bounded-loops            # from PyPI
bl list
```
```bash
# …or from source, today:
git clone https://github.com/qualixar/bounded-loops
cd bounded-loops && pip install -e .
```

**npx — Node convenience wrapper** (still needs Python 3.11+ on your PATH)
```bash
npx bounded-loops list
npx bounded-loops run loops/bug-fix-red-green --yes
```
The npm package is a **thin launcher**: on first run it finds Python 3.11+,
installs the engine, then hands off to the real CLI. It does not reimplement the
tool in Node — Python is the engine, npm is just a convenient front door.

## Quick start (keyless, ~30s)

```bash
pip install -e .
bl run loops/bug-fix-red-green --yes    # keyless stub runner + real pytest gate
```
```
✓ [DONE] gate-passed (laps: 1)  ledger: loops/bug-fix-red-green/.ledger.jsonl
```

See the contrast that makes the point — the same loop, ungated, believing a lie:

```bash
cd loops/bug-fix-red-green && ./wreck.sh    # exits 1: "LIE CONFIRMED"
```

And browse every loop with its role, rung, and gate:

```bash
bl list
```

## Nine bounds (+ kill switch)

Every loop's `bounds.yaml` maps onto nine bounds, enforced across the engine's
layers — not nine flat booleans in one file:

| # | Bound | Enforced by |
|---|---|---|
| 1 | Iteration control — hard lap cap + no-progress stall detection | `bounds.max_iterations` / `bounds.no_progress_window`, in `run_loop.py` |
| 2 | Sandboxing — the agent operates on an isolated scratch **copy** of `seed/`, never the source dir; symlinks refused | `bounds.sandbox` + `composition._make_scratch_workspace` |
| 3 | Input quarantine — secret-bearing files (`.env*`, `.ssh`, `.aws`, `*.pem`/`*.key`, `id_rsa`, `credentials`, `.git`) are **excluded from the sandbox copy**, so a shared loop can't smuggle or exfiltrate credentials | `bounds.quarantine_inputs` (active in `_make_scratch_workspace`) |
| 4 | Output schema validation | `bounds.schema`, consumed by `JsonSchemaGate` when a loop opts in |
| 5 | Tracing — one OTel span per lap, or a no-op tracer when the `otel` extra isn't installed | `bounds.trace` + `TracerPort` |
| 6 | Regression evaluation — satisfied by the loop's **gate choice** (pytest / command / jsonschema / osv / checkov), not a `Bounds` field | the `GatePort` adapter a loop selects |
| 7 | Token budget — real token counts parsed from `claude-code`'s `usage`, accumulated and enforced by `BudgetMeter`. (`shell`/`codex`/`antigravity` report 0 tokens — an honest tool limitation, not a silent gap; `stub`/`python_callable` supply counts from the cassette/glue.) | `bounds.max_tokens` + `BudgetMeter` |
| 8 | Human approval gating | `bounds.require_approval` (derived from `rung` when unset: L1 → none, L2/L3 → required) |
| 9 | Wall-clock timeout — `max_wallclock_s: null` means "use the conservative 1-hour default", **not** "unbounded". It is an **inter-lap** ceiling (checked before each lap); the **in-lap** guard against a single long turn is the runner's and gate's own subprocess `timeout_s` | `bounds.max_wallclock_s` + `BudgetMeter`; runner/gate `timeout_s` |

On top of all nine, an env-var kill switch (`BOUNDED_LOOPS_KILL`) is polled once
per lap, before anything else — the highest-priority stop. And a frozen invariant
runs through all of it: **the engine never trusts the agent's own claim of
"done." Only the gate decides.** The runner's `agent_claimed_done` flag is
recorded but never substituted for a real gate pass.

## Runners and gates

Runners (what proposes a change each lap):

| Runner | What it does | Needs |
|---|---|---|
| `stub` | Replays a recorded cassette of agent turns — fully deterministic, zero external calls. The keyless demos (including the `osv`/`checkov` loops' quick path) use this: the cassette *simulates* the agent's fix so the loop runs offline. To exercise a real agent/scanner, install the tool and use `--runner` or a non-stub cassette | nothing (keyless) |
| `shell` | Pipes the loop's prompt to any CLI command over stdin | whatever CLI you point it at |
| `python_callable` | Calls a `run_turn(prompt, workspace) -> dict` in a spawn-isolated subprocess with a scrubbed env | a Python module implementing the contract |
| `claude-code` | `claude -p --output-format json --bare`; parses real `total_cost_usd` + `usage` tokens | the `claude` CLI **and `ANTHROPIC_API_KEY`/`apiKeyHelper`** — `--bare` never reads OAuth/keychain (confirmed against the real binary) |
| `codex` | `codex exec --json`, sandbox mode derived from rung | the `codex` CLI (JSON schema not yet smoke-tested against a real binary — see Known limitations) |
| `antigravity` | `agy -p`, approval policy derived from rung | the `agy` CLI |

Gates (what decides if a lap is done):

| Gate | Checks | Needs |
|---|---|---|
| `command` | Any command (tokenized, run without a shell — no `\|`/`&&` chaining); exit 0 = pass. Verifies by **exit code only** — for real output validation prefer a typed gate below. A gate needing shell features ships a wrapper script in its loop folder | whatever command you configure |
| `pytest` | `pytest -q` in the workspace | `pytest` |
| `jsonschema` | `workspace/output.json` against a JSON Schema (path from `bounds.schema`) | nothing beyond the core deps (keyless) |
| `osv` | `osv-scanner` reports zero known vulnerabilities (fails **closed** on an empty/garbage report) | [`osv-scanner`](https://github.com/google/osv-scanner) binary **+ network** (fetches the OSV.dev advisory DB) |
| `checkov` | `checkov` reports zero failed IaC checks (fails **closed** on uninterpretable output) | [`checkov`](https://www.checkov.io/) |

Additional gate adapters are pluggable via the composition root; the default
install ships only the universal, keyless-first set above — no gate in any
shipped loop's default manifest requires a paid product.

## Runnable loops (67 folders, across a dozen industries)

Every one is a real folder you run to DONE — a broken seed, a mechanical gate,
the nine bounds, an `L1`/`L2`/`L3` rung, and a `forbid:` guard so the agent
can't rewrite the gate to cheat. **63 of the 67 reach `✓ [DONE] gate-passed`
with zero setup** (keyless stub runner + a real gate); the 4 framework examples
need their framework installed. `bl list` prints them all — here's the spread:

| Industry / role | # | Examples |
|---|---|---|
| Software · backend · dev | 12 | `bug-fix-red-green`, `data-contract`, `openapi-schema-valid`, `dead-import-clean`, `type-annotations-present`, `json-config-schema` |
| Security · supply-chain | 7 | `secret-scan-keyless`, `dependency-pinning`, `dockerfile-no-root`, `cors-not-wildcard`, `jwt-alg-not-none`, `osv-scanner-example` |
| Finance · accounting | 6 | `ledger-reconciliation`, `invoice-3way-match`, `journal-entries-balance`, `iso20022-payment-valid`, `fx-rate-sanity` |
| Legal · compliance | 6 | `citation-existence-check`, `contract-clause-extraction`, `nda-required-clauses`, `gdpr-dpa-terms`, `contract-defined-terms` |
| Content · marketing | 6 | `content-fact-gate`, `frontmatter-schema`, `broken-internal-links`, `alt-text-present`, `reading-level-gate`, `seo-meta-limits` |
| Retail · commerce | 5 | `product-feed-schema`, `price-margin-floor`, `inventory-nonnegative`, `gtin-checkdigit`, `catalog-required-fields` |
| Operations · SRE | 5 | `runbook-completeness`, `oncall-coverage`, `slo-error-budget`, `conventional-commits`, `alert-runbook-link` |
| Enterprise · ERP/SAP | 5 | `idoc-xml-schema`, `transport-request-manifest`, `cds-view-annotations`, `bapi-payload-contract`, `material-master-completeness` |
| Business alignment | 5 | `okr-measurable`, `prd-acceptance-criteria`, `rfc-decision-recorded`, `roadmap-field-contract`, `meeting-action-items` |
| Testing · QA | 4 | `test-naming-contract`, `assertion-density`, `no-hardcoded-sleep`, `test-presence-per-module` |
| Research pipeline | 4 | `claim-source-mapping`, `dataset-license-allowed`, `reproducibility-manifest`, `bibliography-completeness` |
| Healthcare · frontend | 2 | `clinical-note-completeness`, `a11y` |

The flagship for "AI reliability" is [`citation-existence-check`](loops/citation-existence-check/):
a runnable form of the single most-documented AI failure in law — fabricated
case citations (1,600+ sanction cases catalogued by mid-2026) — where the gate
fails until every cited case resolves to a real one.

Every loop's own README shows the "prove the gate genuinely fails on the unfixed
seed, then passes on the fix" walkthrough with **actual captured output** — run,
not asserted. The wider [recipe catalog](catalog/README.md) lists more loop
recipes across these same industries (each naming a real gate) to wire up next.

On accessibility specifically: `axe-core`/Lighthouse need a running browser, and
no keyless *universal* a11y tool exists — so the `a11y` loop ships a small,
dependency-free static linter (missing `alt`, missing `lang`, unlabeled inputs).
It's honest about catching the static WCAG subset, not rendered-DOM issues.

## Scaffolding your own loop

```bash
bl new --list                    # pytest-basic, jest-basic, go-test-basic,
                                 # cargo-test-basic, rspec-basic, junit-basic
bl new pytest-basic my-loop      # <template> <destination> (positional)
```

## MCP server and IDE plugins

`bounded-loops-mcp` exposes `bl_run`/`bl_lint`/`bl_list` as MCP tools — a thin
shim over the same engine the CLI uses. `bl_run`'s `confirm` is a real,
server-side-enforced gate: it binds the full run identity (gate **+ runner +
iteration cap**), so a caller can't preview a safe `shell` runner and then
confirm a credentialed one; and a loop needing interactive approval is refused
before it's ever wired. `plugins/{claude-code,codex,antigravity}/` each ship the
MCP config + an `AGENTS.md` snippet + a verify-on-stop hook.

## Known limitations (stated plainly, not hidden)

- The `codex` runner's real-binary JSON schema is not yet smoke-tested (no
  `codex` install at build time); its token accounting reports 0 until verified.
- `langgraph-example`'s glue hard-codes `changed: True` — its no-progress bound
  can't fire for that example; a real integration should diff before/after.
- Two gates are **not** offline, and require network at check time:
  `content-fact-gate` (link liveness) and the `osv` gate (`osv-scanner` fetches
  the OSV.dev advisory database). The "~30-second, no-API-key, fully offline"
  quick-start refers to the default `bug-fix-red-green` loop specifically
  (stub runner + pytest gate), which needs neither network nor a key. The
  keyless-first set — stub/shell runners with pytest/jsonschema/command gates —
  is offline; the security-scanner gates (`osv`, `checkov`) bring their own
  binary and, for `osv`, a network fetch.
- `python_callable` always spawns and scrubs the child env to a small allowlist —
  by design, but your callable's import side effects run in a clean environment.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) — `bl new` a template, replace the seed,
prove your gate genuinely fails then genuinely passes, and open a PR. The bar for
a catalog recipe or a loop: a **real** gate and a **testable** done-condition,
never "an LLM decides."

## Citation

bounded-loops is research-grade software from the **Qualixar AI Reliability
Engineering** initiative. If it's useful in your work, please cite it — GitHub's
"Cite this repository" button reads [`CITATION.cff`](CITATION.cff):

```bibtex
@software{bhardwaj_bounded_loops_2026,
  author    = {Bhardwaj, Varun Pratap},
  title     = {bounded-loops: runnable, bounded AI-agent loops},
  year      = {2026},
  publisher = {Qualixar},
  version   = {0.1.0},
  url       = {https://github.com/qualixar/bounded-loops}
}
```

## Security

Found a way to trick the gate or escape a bound? That's the most valuable bug we
can get — please report it privately per [SECURITY.md](SECURITY.md).

## License

[Apache-2.0](LICENSE). Copyright &copy; 2026 Varun Pratap Bhardwaj / Qualixar.

---

## Part of the Qualixar AI Reliability Engineering initiative

bounded-loops is one product in [Qualixar](https://qualixar.com)'s open-source
platform for making AI agents reliable:

| Product | Purpose | Install |
|---|---|---|
| **[SuperLocalMemory](https://github.com/qualixar/superlocalmemory)** | Local-first agent memory + learning | `npm install superlocalmemory` |
| **[Qualixar OS](https://github.com/qualixar/qualixar-os)** | Universal agent runtime | `npx qualixar-os` |
| **[SLM Mesh](https://github.com/qualixar/slm-mesh)** | P2P coordination across sessions | `npm i slm-mesh` |
| **[SLM MCP Hub](https://github.com/qualixar/slm-mcp-hub)** | Federate 430+ MCP tools | `pip install slm-mcp-hub` |
| **[AgentAssay](https://github.com/qualixar/agentassay)** | Token-efficient agent testing | `pip install agentassay` |
| **[AgentAssert](https://github.com/qualixar/agentassert-abc)** | Behavioral contracts + drift detection | `pip install agentassert` |
| **[SkillFortify](https://github.com/qualixar/skillfortify)** | Formal verification for agent skills | `pip install skillfortify` |
| **[Agent Amplifier](https://github.com/qualixar/agent-amplifier)** | Runtime amplification hooks | `pip install agent-amplifier` |
| **bounded-loops** *(this repo)* | Runnable, gate-verified agent loops | `pip install bounded-loops` |

**Local-first. Honest about what runs. Built to complement the ecosystem, not fence it off.**

Start here → **[qualixar.com](https://qualixar.com)** · Author: **[Varun Pratap Bhardwaj](https://qualixar.com)** — founder, Qualixar.

---

<p align="center">
  If bounded-loops saves you from an agent that lies about being done,<br/>
  <a href="https://github.com/qualixar/bounded-loops">⭐ <strong>star it on GitHub</strong></a> — it helps other developers find it.
</p>
