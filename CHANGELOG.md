# Changelog

All notable changes to bounded-loops are documented here. This project follows
[Semantic Versioning](https://semver.org/).

## [0.6.5] — 2026-08-17

### Fixed

- **`max_wallclock_s` is now enforced inside an attempt, not only between them.** The ceiling was
  compared against elapsed time at the top of each lap and nowhere else, so it bounded the gap
  between attempts rather than an attempt. A loop declaring `max_wallclock_s: 120` was observed
  running one attempt for over 300 seconds and being stopped, in the end, by a runner default it
  had never declared. The number was in the manifest and readable; it just did not constrain the
  run.

  What the bound now promises, stated exactly: **worker time is bounded by `max_wallclock_s`, and
  total run time by `max_wallclock_s` plus at most one gate timeout.** Gates are deliberately not
  clamped — cutting a gate short yields no verdict, and a verdict that cost a few extra seconds is
  worth strictly more than none.

  Exceeding the declared ceiling now **halts** the run and names the bound. Exceeding a runner's own
  `timeout_s` remains a runner **error**. A run that stopped because it was told to is not a run
  that broke, and they no longer report the same way.

- **Every shipped loop's wallclock ceiling was wrong, and enforcing the bound is what revealed it.**
  65 of 69 loops declared a 60-second total budget against `max_iterations: 10` — six seconds per
  attempt. The demo worker finishes a lap in about 0.4s, so nothing noticed. Measured across four
  providers on four loops, one real agent turn takes 20–271 seconds. Ceilings are now
  `max_iterations × 90 + handoff_reserve_s`, and a test enforces the rule catalogue-wide.

- **Two runners never told the agent what it was forbidden to touch.** Seven runners each carried a
  `_build_prompt`, three annotated as verbatim copies; they had drifted into four variants, and the
  `docker` and `worktree` versions dropped `spec.forbid` from the assembled prompt entirely. A loop
  with no `PROMPT.md` therefore spent attempts being refused for touching files it was never told
  about. One shared definition now, with a test that refuses to let a runner define its own again.

- The agent-CLI runners' default per-turn timeout is 600s, up from 300s. One measured turn was still
  working when 300s killed it, so 300 was truncating progress rather than catching a hang.

### Added

- **A bound halt leaves a handoff instead of only a reason.** `HANDOFF.md` is written beside the
  ledger on every bound halt, from facts the harness already holds: which bound fired, attempts
  spent, which laps changed the workspace, the gate's last message, and whether the run was stuck or
  merely short of budget. It costs nothing and cannot be wrong about what happened.

- **`bounds.handoff_reserve_s`** (default 90, `0` declines) gives the agent one final turn to say
  *why* — what it did, what is left, what it would do next. **The reserve is taken out of
  `max_wallclock_s`, never added to it:** work gets `ceiling − reserve`, the wind-down gets the
  reserve, and the declared total is unchanged, so every termination guarantee holds verbatim.
  Granting the turn *after* the ceiling would have made the ceiling silently mean "plus however long
  a summary takes".

  Without this, a run that hit its ceiling mid-task discarded everything the attempt had worked out,
  and the next run began from the same seed with the same budget and no knowledge of it — so a task
  needing more than one budget window could never finish, however often it was run.

  The kill switch gets no wind-down: an operator pulling it wants the run to stop now, not to spend
  more of anything. Every other bound halt gets one, no-progress included, since a stuck agent's
  account of what it tried is the most useful handoff of the set. Nothing in the wind-down can
  change the terminal status.

- `attempted` on every ledger row, so bound utilisation is auditable from the receipt. A ceiling
  halt at `max_iterations: 10` writes an eleventh row, and anyone computing consumed-over-declared
  previously read 1.1 for a bound that held exactly.

### Changed

- Change detection is content-addressed (`adapters/runners/workspace_digest.py`), replacing six
  mirrored copies of a git-based detector that could not report "unchanged" after lap 1 — the
  no-progress soft bound was inoperative for every runner that shells out. **git is no longer a hard
  engine dependency.**
- The controller's attempt and repair round are published into the loop workspace, so a repair round
  can change an outcome; the graph work bound's `(1+R)` factor had never been exercised.
- New loop `record-completeness`; catalogue 68 → 69, keyless 64 → 65.
- Existence obligations in five gates that had certified emptied artifacts.
- Gate violation-count contract, with an explicit predicate-gate allow-list.
- A compile check over all shipped gate scripts — `loops/` had never been linted.

## [0.6.4] — 2026-08-16

### Changed

- **A gate that reads your work and finds nothing to check now FAILS the attempt instead of
  ending the run.** Twenty-four shipped checkers reported an empty, vacuous or malformed artifact
  with exit `2` — the code reserved for "the gate could not run" — so the engine treated a precise,
  actionable diagnosis as an infrastructure fault and halted. Writing broken JSON stopped your run
  rather than telling you what was wrong.

  The line is now stated: anything wrong with an artifact **you own** is a rejection you can act
  on; exit `2` is reserved for the gate itself being unable to run — a bad argv, or a file the loop
  ships that you were never allowed to edit. Your loop's `forbid` list already declares which is
  which. See [`docs/gate-verdict-contract.md`](docs/gate-verdict-contract.md).

- **`pytest` gates: a failed test collection is now a failed check, not a broken run.** `pytest`
  returns exit `2` both for "your module does not import" and for "somebody interrupted me", and
  the first was being reported as the second. Emptying the module under test is the most common way
  an edit goes wrong in a bounded loop, and it now comes back to the worker with the import error.
  A genuine interruption still halts.

### Changed (metrics)

- **`bl graph metrics` now reports an anytime-valid confidence sequence.** The stitched PrPl-EB
  sequence had been implemented and tested since 0.6.0 and was called by nothing: every published
  interval came from the fixed-time empirical-Bernstein radius, which is not valid under the
  optional stopping a live run performs by construction. Watching a run means peeking after every
  observation, and that is precisely what the old radius did not survive.

  Measured on the pooled multi-node sequence the product actually builds, the replacement gains 12
  to 23 coverage points at every regime simulated.

- **Every interval now names its ESTIMAND, not just its level.** The text label reads
  `anytime-valid 95% for-log-mean`, the JSON carries `interval_estimand`, and
  `INDEPENDENCE_CAVEAT` states it in prose. What the sequence brackets is the mean false-accept
  propensity of the attempts **in this log** — the right quantity for auditing a receipt stream —
  and *not* the population rate of a future workload.

  This distinction was previously carried as a caveat and got quoted away from it. Measured
  coverage of the log mean is 1.0000 across every regime; coverage of the population marginal rate
  is a different number ranging 0.83–1.00 with the count of independent nodes pooled and their
  heterogeneity.

- **JSON: the interval block is now `anytime_valid`.** `empirical_bernstein` remains as a
  deprecated alias to the same object and will be removed in a future major. A key named for a
  retired method is how a consumer ends up quoting the wrong guarantee.

### Fixed

- **The 0.5850 marginal-coverage figure was a corner case presented as a general result.** It is
  the value for a sequence carrying a *single* latent propensity — a run with one node — while the
  product pools attempts across many. On six nodes the same estimator reaches 0.8450, on thirty
  0.9783. The figure was accurate and its scope was not, which is the same defect class as the
  gate bugs fixed in 0.6.2.

- **Sixteen more catalog gates accepted work that violates the task they state.** Every one had the
  same shape as the fourteen closed in 0.6.2 — the gate agreeing with the thing it exists to catch:

  - `test-presence-per-module` accepted a test file with no import and no assertion, a test for one
    module that imports a different one, a test that asserts on a standalone calculation it never
    feeds through the module, and a rename in the source that leaves the test importing a name no
    longer there. It now checks that the test imports the module, uses what it imported, asserts
    something, and that the name it imports exists.
  - `nda-required-clauses` and `gdpr-dpa-terms` read section headings and never section bodies, so a
    clause could keep its heading and say the opposite — `## Confidentiality` over "Neither party
    shall be required to hold the other party's Confidential Information in confidence".
  - `okr-measurable`, `dependency-pinning` and `conventional-commits` allowed an item to be deleted
    rather than repaired. "Every key result is measurable" was satisfiable by removing the vague one.
  - `dependency-pinning` also accepted a pin to a version that was never released, and a pin below
    the floor the file itself declared. It now checks both, keylessly, against shipped reference
    data — the same approach `citation-existence-check` already uses.
  - `conventional-commits` accepted a subject rewritten into an unrelated change, a documentation
    change typed as `build`, and edits to subjects that already conformed and were never in scope.
  - `secret-scan-keyless` missed a credential split across a concatenation (`"AKIA" + "..."`) and a
    `password` renamed to `passwd`, and allowed the config to be deleted rather than moved to the
    environment.

- **`ledger-reconciliation` accepted an emptied ledger.** An empty ledger balances (`0.0 == 0.0`)
  and every transaction in it is categorised, so deleting the books was the cheapest way to make
  them reconcile. `PROMPT.md` forbids deleting rows; nothing checked it.

### Notes

- Two guard tests that forbade the string "anytime-valid" were **inverted rather than deleted**:
  they now require it. They existed to stop a false claim, and the direction of the possible lie
  has reversed, not disappeared.
- `pytest -m provider_smoke` collects **zero tests**. The marker is defined and documented as
  contacting a real provider account, and nothing carries it. Recorded here because the gap is
  real and is not closed by this release.

## [0.6.3] — 2026-08-15

### Fixed

- **`bl_graph_evidence` advertised the wrong argument name.** Its published MCP schema said
  `run_id`, while `bl_graph_terminal_runs` returns the address as `run_ref`, the documentation
  says to pass `run_ref`, and the resolver wants the directory name. A consumer reading the
  tool schema would pass the run's identity — which does not resolve, because a run usually
  lives in a directory named something else. The tool did the right thing under the wrong name.

  The argument is now `run_ref`. No change to resolution behaviour, evidence shape, contract
  id, trust semantics, or terminal-run handling. A test now asserts the real registered
  signature, and another performs the full round trip: whatever the listing returns as
  `run_ref` must work as the fetch argument.

  Found by the SuperLocalMemory 4.0.4 bridge audit.

## [0.6.2] — 2026-08-15

### Fixed

Fourteen shipped gates accepted output they should have rejected, or rejected output they
should have accepted. Each was confirmed by running the gate, and each now has a regression
test in both directions.

**Gates that passed genuinely broken work:**

- `citation-existence-check` — a fabricated case in a reporter absent from the trusted file
  was never examined at all, because the citation pattern was built from that file. Inventing
  a reporter was easier than inventing a page number. An unverifiable citation now fails.
- `dockerfile-no-root` — `USER root` after a non-root `USER` passed. Only the last `USER` in
  the final build stage decides who the container runs as, and that is what is checked now.
- `gdpr-dpa-terms`, `nda-required-clauses`, `privacy-policy-completeness` — a document
  *denying* a required term satisfied it, because any mention anywhere counted. Each required
  term must now have its own section heading.
- `runbook-completeness`, `rfc-decision-recorded` — a document of bare headings with nothing
  written under them passed. Sections must now have content; sub-headings count as content.
- `alt-text-present` — an HTML `<img>` with no `alt` was invisible; only markdown images were
  checked. Both are checked now.
- `broken-internal-links` — an HTML `<a href>` to a missing file was invisible.
- `secret-scan-keyless` — a password in a dict or JSON mapping was invisible; only
  `name = "value"` assignments were scanned.
- `cds-view-annotations` — commented-out annotations counted as present.
- `assertion-density`, `test-naming-contract` — `async def` tests were invisible.
- `okr-measurable` — an objective with no key results at all passed.

**Gates that blocked correct work:**

- `conventional-commits` — `feat!:` was rejected. The `!` breaking-change marker is part of
  Conventional Commits; `revert` and `style` were also missing from the accepted types.
- `dependency-pinning` — `requests[security]==2.31.0`, `urllib3 == 2.0.7`, `torch==2.1.0+cpu`
  and environment markers were rejected as unpinned. All are exact pins. `foo==1.0.*` is a
  prefix match rather than a pin and is still rejected.

### Added

- **A stable evidence contract, `bounded-loops.dev/slm-bridge/v1`.** Another product can now
  observe a finished graph run over MCP without importing this package, parsing its receipt
  files, or pinning its version. Two read-only tools: `bl_graph_terminal_runs` lists what has
  finished, `bl_graph_evidence` returns one run's outcome, digests, node states, attempt counts
  and receipt head. `bl_capabilities` advertises the contract so a consumer can discover it.

  Compatibility is the contract id, not our version number — branch on it and stay on the
  latest release of both products. SuperLocalMemory is the first consumer and an entirely
  optional one; neither product depends on the other, and each is complete installed alone.

  Gate prose, artifact contents, paths, commands, environment values and secrets never appear
  in the document. `workspace_id` is a digest rather than a path. Non-terminal runs and unsafe
  run references are refused. `eligible_for_learning` is `false` in the payload, and
  `demonstration` marks a cassette replay — the evidence supports observation, not learning.

  See [`docs/evidence-contract.md`](docs/evidence-contract.md).

- Every loop in the catalog now has an end-to-end test that runs by default: it must reach
  DONE, and its untouched seed must fail its own gate. Previously ten loops had such a test
  and all ten were excluded from the default run, so a broken loop could ship green. The
  whole catalog takes about 20 seconds.

## [0.6.1] — 2026-08-15

### Fixed

- **`pip install bounded-loops` now comes with the loops it advertises.** The catalog lived
  only in the git repository, so a pip-only user ran `bl loops list`, saw "No loops found",
  and was told to "run from a bounded-loops source checkout" — by a package whose README
  opens by advertising 68 of them. The wheel now carries the catalog, and `bl loops install
  <name>` copies one into your project.

  Bundled rather than downloaded on demand, deliberately. A catalog that needed github.com
  would be unavailable in the air-gapped, corporate-proxied and egress-restricted
  environments this engine is aimed at, and the whole posture is that it runs offline with no
  credential. 2.5 MB in the wheel is the cheaper honesty.

  Installing is a separate step from bundling because `bl run` writes its ledger BESIDE the
  loop. `site-packages` is not writable in a managed environment and is not a sensible place
  to accumulate one user's run receipts. `bl loops install` puts the loop in the project
  workspace, which is where a run's evidence belongs. `--overwrite` refuses any target that is
  not itself a loop package, and the loop name is validated as one path segment before it can
  reach a delete.

- **README images rendered broken on PyPI.** `readme = "README.md"` makes that file the PyPI
  project page, and PyPI does not resolve relative image paths the way GitHub does. The
  architecture diagram had been relative since it was added, so it was broken on the page most
  people reach from `pip install` for every prior release — while looking correct in the editor
  and on GitHub the whole time. Two tests now cover it: one for relative paths, one for
  absolute URLs pointing at files that were never committed.

- **Switching runs in the monitor left a node from the other run's graph selected.** The
  Configure panel stayed headed with a node the displayed graph did not contain. The saved-graph
  path already cleared this, with a comment explaining why; the run path never did.

### Changed

- `hatchling` is pinned. Unpinned it emitted `Metadata-Version: 2.5`, which twine rejects and
  whose PyPI acceptance could not be confirmed.


## [0.6.0] — 2026-08-15

Two independent auditors went through this release end to end, five focused passes each. Eight
HIGH findings survived verification; all eight are fixed below, along with everything they found
at MEDIUM and LOW. Every fix carries a test that was checked by re-introducing the bug.

### Fixed — the MCP server

- **`bl_run(confirm=true)` could never execute.** The preview→confirm handshake keyed its state
  on the MCP session object, and MCP 2.0 builds a new `ServerSession` for every request — so the
  preview landed under one key and the confirm looked under another. Every confirm came back
  "no matching preview", from every host, on both protocol eras. The tool's core function was
  unreachable on the transport it ships over.

  The handshake is now a signed token: `confirm=false` returns a `confirm_token`, and
  `confirm=true` requires it. It is an HMAC over the run's full executable identity — gate
  command, runner, agent_cmd, cassette, iteration cap, run_id, resume, and a content hash of the
  loop's files — signed with a per-process secret and valid for 15 minutes. It works identically
  on stdio, HTTP, stateless or pooled, and it closes the old fallback path in which one client
  could confirm another client's preview.

  **This changes the tool contract.** A caller that passes `confirm=true` without a token is
  refused, and told what to do. Nothing that worked before stops working, because nothing
  worked before.

### Fixed — surfaces that claimed more than the receipts support

- **`bl graph status` and `bl graph metrics` refused runs they had just watched succeed**, with
  "package digest is not admitted". Four reload sites passed no admitted loop packages, and that
  parameter defaults to the empty set, so forgetting it produced a confident wrong answer rather
  than an error. An AST check now fails the build if any caller omits it.
- **STATE.md ended a run with "all nodes succeeded"** while the node table printed directly above
  it showed a SKIPPED branch. It now says how many branches were not taken, and names them.
- **The Arena drew "Gate passed — an independent check confirmed the result" on any SUCCEEDED
  node.** An approval node succeeds because a human held it, with no gate verdict in the log at
  all. The badge now reads the verdict it was already being handed.
- **The confirm screen listed only `irreversible` and `financial` as effects that cannot be
  undone**, so a graph whose publish node declares `external_write` — every shipped publish graph
  — showed an empty list under a sentence about work that cannot be undone. Both this and the
  configuration interview now take the set from the domain.
- **An approval preview said only "approved approval node 'gate'".** Approving a gate releases
  everything downstream of it, which is what the recorded grant has always contained. The preview
  now names those effects and flags the irreversible ones, and the monitor renders them.
- **A loop declaring an isolation tier this host cannot deliver was started anyway**, then failed
  at the node — while `bl_capabilities` promised refusal before the run starts. The pre-run gate
  had exempted loop nodes using the connector-transport predicate; a loop needs no transport
  while very much running in a sandbox.
- **The Seatbelt probe checked whether `sandbox-exec` is executable**, which is vacuously true
  inside a nested sandbox where applying a profile fails. It now applies one and reads the exit
  status.

### Fixed — durability and safety

- **A run killed mid-flight left receipts nothing could open.** `run-meta.json`, `plan.json` and
  the manifest were written after the work finished, so a hard kill — including Ctrl-C on
  `bl monitor`, whose execute route runs on a daemon thread — produced a hash-valid log that
  every surface refused. Those files describe the plan, not the outcome, so they are now written
  before the first node runs.
- **`graph.save` wrote through a symlink.** It resolved the path and then asked whether it was a
  symlink, a question that can only answer False once the link has been followed. An alias inside
  the workspace silently overwrote the file it pointed at while reporting the alias was saved.
- **The string `"false"` counted as confirmation** and started runs. Confirmation now requires
  the boolean.
- **Two confirms in the same second could mint the same run directory**, and `exist_ok=True`
  swallowed it: both callers were told the run started, and both then watched the first run's
  receipts.
- **Symlinked directories were advertised as runs**, and the refusal to open one escaped as a
  closed socket plus a traceback on the operator's terminal.
- Monitor pages now carry a Content-Security-Policy and `X-Frame-Options: DENY`.

### Fixed — the host pack

- **Every shipped command named MCP tools that were never registered** — `bl_graph_status` for
  `graph_status`, and wrong parameter names throughout. The contract test only read `SKILL.md`,
  so the primary product path was broken and nothing said so. All corrected, and the test now
  checks every command and agent against the live registry.
- **`bl graph digest` did not exist** although the composer agent instructed models to run it to
  obtain the one field they are forbidden to invent. It exists, and `bl_catalog` now carries the
  digest too.

### Changed

- **MCP 2.0.** `mcp>=2,<3`, protocol revision `2026-07-28`, with 2025-era clients still served
  from the same process. The 1.x line went maintenance-only, and `mcp.server.fastmcp` — which the
  old pin depended on — no longer exists.
- **Jarvis is now the bounded-loops monitor.** `bl monitor` is unchanged; the package moved.

### Added

- `bl graph digest <loop-dir>` — the content digest a loop node must reference.
- `/bl-configure` — walks a saved graph's interview and applies the answers.
- `scripts/sync_host_pack.py` — mirrors the canonical host pack to all three hosts. The contract
  test told developers to run this for some time before it existed.
- Approval receipts now record **who** decided, not only which tenant, with the source of that
  name and an explicit note that it is not authenticated on a local run.

### Added — the project home and the monitor (built for this release)

- **`.bounded-loops/` is now a project home.** `bl init` creates it; `bl where` prints the
  resolved workspace and, more usefully, *why* that one was chosen. It holds `config.toml`,
  `graphs/`, `loops/`, `runs/`, `tickets/`, and an `index.json` cache. One resolver answers
  "where does this project keep its runs" for the CLI, MCP, and the UI — the same question
  answered twice is the defect class the 0.5 audits kept finding.

  Discovery walks up from the current directory for an existing `.bounded-loops/`, **bounded by
  the git repository root** so a checkout can never silently borrow a workspace sitting above it,
  then falls back to the repository root, then to the current directory. A symlinked workspace
  root is refused: it would silently relocate every receipt in the project.

- **A capability contract, over MCP and on the command line.** `bl_capabilities` (MCP) and
  `bl capabilities` (CLI) serve the same document from one function: node kinds with their
  kind-specific fields, gate kinds and what each *mechanically* checks plus whether it is
  available on this host, isolation tiers with the controls actually enforced here, which failure
  policies are **honoured** versus merely **declared**, the repair contract and its bound,
  the effect vocabulary, every budget field and where it is enforced, the terminal statuses and
  which of them are not success, and all 37 refusals with the fix for each.

  This is the document a host model reads instead of guessing. Two rules govern it: declared is
  not honoured, and *here* is not *everywhere*.

- **`bl_catalog` and `bl_search_loops` (MCP).** The loop catalog with role/gate/keyless filters,
  and a ranked search against a described task. The ranking is **lexical** and the response says
  so — it matches words, it does not understand meaning.

- **A refusal reference.** Every one of the 37 validator refusals now has a plain-language
  summary and an actionable fix, checked against `validate_graph.py`'s own source in both
  directions so the table cannot document a refusal that cannot happen, or miss one that can.
  Readable as `bl capabilities --refusals`.

### Fixed

- **The authoring schema advertised two failure policies the compiler refuses.** `on_failure`'s
  enum offers `continue` and `await_human`, but the validator rejects both with
  `on_failure_unimplemented` — correctly, since the runtime routes every failure to `fail_graph`
  and accepting them would silently discard the declared policy. Anything generating an authoring
  UI from the schema would have offered them as choices. The schema now carries
  `x-unimplemented`, and a drift test pins it to the validator's own set. `isolation` likewise
  carries `x-never-available` for `customer_managed_worker`, which no host can enforce.

- **`bl graph run --execute` no longer requires `--out`.** It defaults to
  `.bounded-loops/runs/<stamp>-<rand>/`, creating the workspace if needed, and announces the
  resolved path on stderr. An explicit `--out` behaves exactly as it did in 0.4.0, and **no
  existing run directory moves** — `bl run --run-id` still writes package-local, so every
  0.4.0/0.5.x run stays resumable under `--resume` and `bl runs`.

## [0.5.1] — 2026-08-14

### Fixed

- **`import bounded_loops` failed on Python 3.11.** A dataclass field defaulted to
  `MappingProxyType({})`, which reads as safe because it is immutable — but 3.11's dataclasses
  reject any default whose class is unhashable, and `mappingproxy` only became hashable in 3.12.
  The class body therefore raised at import time on the oldest Python this package supports.
  0.5.0 is unusable on 3.11 and should be skipped.

- **Reference-graph digests did not match a fresh clone.** `bl run <package>` writes
  `.ledger.jsonl` into the package directory, and that file was not excluded from the package
  content digest — so any machine that had followed the README quickstart digested
  `bug-fix-red-green` differently from a clean checkout, and the committed graph pins were generated
  on such a machine. `.ledger.jsonl` and `.trust.json` are now excluded, and a test asserts that no
  digested entry in a shipped package is untracked.

- **A live isolation-provider test failed instead of skipping on hosts without the capability**,
  which made a red CI build the normal state from 0.4.0 onward.

## [0.5.0] — 2026-08-14

### Changed — BREAKING for embedders

- **`NodeWorkerPort` and `IndependentGatePort` gained required arguments.** `execute` now takes
  `repair_round`; `evaluate` now takes `attempt` and `repair_round`. Both are keyword-only and have
  no default, so a custom worker or gate raises `TypeError` until it accepts them. Add the
  parameters — one line per implementation — and ignore the values if you do not need them. See
  [docs/EMBEDDING.md](docs/EMBEDDING.md).

  Required rather than defaulted, because a default here is a silent wrong answer: attempts RESET at
  a repair boundary, so `(node, attempt=1)` happens once per round and `attempt` alone is not an
  identity. Two things this unblocks:

  - **A `kind: loop` node may now declare `on_failure: repair`.** It was refused at validation
    before — the round could not reach a loop worker, so the receipt would have named round 0 for
    every round, and a false round inside a hash-chained log is worse than a refusal.
  - **The loop gate verifies the receipt's attempt.** A receipt claiming `attempt=99` used to pass,
    because `evaluate` had no attempt to compare against.

### Changed

- **The Wilson comparison figure is now the one the test suite reproduces.** Every mention of
  Wilson's measured coverage said **31–41%**, attributed to "real retry data". Both halves were
  wrong: it came from a reviewer's separate simulation whose parameters were never recorded, and the
  shipped harness cannot reproduce it at any correlation strength (Wilson measures 0.75–0.80 across
  ρ ∈ [1.0, 3.5]). It now reads **77.5%**, from the seeded simulation in
  `tests/graph/application/test_confidence_sequence.py`.

- **A `plan_id` mismatch on resume now says which explanation applies.** The message reported two
  digests, which is also what a tampered run directory looks like, so an engine upgrade sent users
  hunting for an edit that never happened. Run directories record `compiler_version`, and the error
  distinguishes a compiler change from a modified directory.

- **Gate false-accept rate intervals now report measured coverage instead of an assumed one.**
  The Wilson score interval required independent Bernoulli trials, which retried
  attempts violate. It is replaced by an empirical-Bernstein interval with a
  predictable plug-in. The `bl graph metrics` label changes from
  `nominal-95% iid (UNCALIBRATED)` to `emp-Bernstein 95% (COVERAGE-MEASURED)`.

  **Both measured numbers are coverage of the same thing, and it is not α.** Under
  the simulated regime — per-run latent rate `p_run ~ logit-normal(mean α, ρ=1.8)`,
  evaluated at every sample size under optional stopping — Wilson covered `p_run`
  77.5% of the time and the empirical-Bernstein interval covered it 96.9%. The
  quantity `bl graph metrics` reports as the false-accept rate is the *marginal*
  rate `E[p_run]`; coverage of the marginal rate is a **separate estimand that
  these figures do not measure**. So "96.9% vs 77.5%" is a statement about
  `p_run`, and quoting it as "α coverage" or as a 19-point improvement on α is a
  misreading the numbers cannot support. Measured on the same simulation: coverage of
  the marginal rate is **0.5850**. For the quantity `bl graph metrics` prints, this is
  a 58.5% interval, not a 95% one.

  **This is NOT an anytime-valid confidence sequence.** The radius is the
  fixed-time empirical-Bernstein form and carries no stitching term, so
  simultaneous validity over all sample sizes does not follow from it.

### Fixed

- **A 0.4.0 graph with a `publish` node can resume again.** The compiler began carrying
  `publication_policy` in the plan so the publish worker could read it, which changed `plan_id` for
  every graph that had a publish node — and those are the graphs with an irreversible effect. The
  value is authored in the manifest and therefore already covered by `source_graph_digest`, so it is
  excluded from the plan's canonical form. Verified against v0.4.0's own compiler: the same graph
  compiles to the same `plan_id` it did in 0.4.0.

- **An empty directory inside a loop package now moves its content digest.** `shutil.copytree`
  reproduces empty directories into the workspace, so a gate whose `run:` branched on
  `test -d seed/hidden_branch` could change verdict while the pinned digest stayed fixed — a mutable
  region under a content address.

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

- **`kind: loop` nodes run.** A graph node can now be a whole bounded loop, executed as a child
  workflow. 0.4.0 accepted these nodes at compile and lint time and then refused them at preflight;
  they now execute.

  The package is pinned by **content digest**, not by name: `loop_package: sha256:<64 hex>` is
  computed from the package's own bytes, re-verified inside the node's subprocess before the loop
  runs, and resolution is by digest only — so pulling new commits cannot silently change what a
  persisted `plan_id` executes. Isolation is per node and never defaulted; the node's sandbox wraps
  the loop's own runner and gate, so the loop inherits the graph's execution envelope.

  The outer gate verifies the loop's **receipt** — that the promoted outcome parses, names the
  package the plan admitted, names this node, attempt and repair round, and reached `DONE`. It does
  not re-run the loop's own gate: the loop already contains an independent gate, so re-running it
  would make one object both producer and judge.

- **`kind: join` and `kind: publish` nodes have workers and gates.** A join records the live state
  and guard of every predecessor it observed, and its gate replays the scheduler's own admission
  predicate rather than trusting the receipt. A publish node is the one place a graph may do
  something it cannot undo; its effect is recorded in a publication ledger keyed on
  `run_id / plan_id / node_id` — `attempt` and `repair_round` are excluded on purpose, because
  including either would fire the effect again per attempt or per round — with a payload digest
  over the upstream artifacts.
  See `docs/graph-capabilities.md` section 14, including what the local ledger does **not**
  guarantee.

- **Six reference graphs, in `graphs/`.** Finance payment assurance, engineering release gate, retail
  listing release, marketing campaign release, customer data request, solo-builder ship. Each is
  fan-out to parallel loop checks → join → human approval → one irreversible publish, uses only
  keyless shipped loops, and costs nothing to run. All six execute end to end in CI, from a checkout.

- **Wire data between a loop and a graph.** A loop package may declare `inputs:` and `outputs:` port
  blocks in its `loop.yaml`; the engine materialises each declared input before the loop starts and
  promotes each declared output as a graph artifact afterwards. A loop that declares neither runs in
  fixture mode, exactly as before. See [docs/EMBEDDING.md](docs/EMBEDDING.md).

- **A documented embedding surface.** `bounded_loops.__all__` is the stable API — `load_loop`,
  `wire`, `Bounds`, `Outcome`, `Status`, `LoopManifest`, plus `NodeWorkerPort`, `WorkerResult`,
  `IndependentGatePort` and `GateVerdict` for plugging in your own worker or gate. Everything else is
  internal and may change in any release. [docs/EMBEDDING.md](docs/EMBEDDING.md) is the walkthrough.

- **`--loop-roots <dir>`** on `bl graph run`, `lint` and `plan`, repeatable, to add your own catalog
  of loop packages. Resolution stays by digest, so an extra root can only make a package findable —
  never redirect an admitted digest to different code.

- **Real spend ceilings.** A run can declare token and cost caps, from a file or per-dimension
  flags, and a node that declares a spend budget refuses to run on a worker that reports no usage
  rather than metering it as free.

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
