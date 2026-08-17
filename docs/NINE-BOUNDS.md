# The nine bounds (+ kill switch)

Every loop's `bounds.yaml` maps onto nine enforced bounds. They are not nine
flat booleans living in one adapter — they are enforced across the engine's
layers, which is the point: no single misconfigured or malicious adapter can
quietly disable one. This document expands the mapping table from the
top-level README with the exact `bounds.yaml` field, the exact component
that enforces it, and why each bound exists.

## 1. Iteration control — hard lap cap + no-progress stall detection

**Field:** `bounds.max_iterations` (required, hard ceiling of 1000 enforced
in `manifest.py`, non-overridable in v1) and `bounds.no_progress_window`
(default 3). **Enforced by:** `RunLoopUseCase.run()` in `run_loop.py`, which
calls `BudgetMeterPort.exceeded(lap, bounds)` at the top of every lap for
the hard cap, and `BoundsEnforcer.check_no_progress(bounds)` (delegating to
the pure `domain.rules.no_progress`) after each failed gate check for the
stall case.

Why it matters: an ungated agent loop's most common failure mode isn't a
crash, it's an agent that keeps "trying" forever — burning tokens and wall
time while never converging. A hard lap cap turns "runs forever" into "fails
loudly at lap N," and the no-progress window catches the subtler case where
the agent is still running but has stopped actually changing anything
(`RunResult.changed == False` for `window` consecutive laps) — a spin, not
a stop.

**How "changed" is decided, and why it used to be wrong.** `changed` comes
from a content-addressed digest of the whole workspace, taken before the turn
and again after it, within the same lap
(`adapters/runners/workspace_digest.py`). Harness-written files —
`agent_output.txt`, the ledger, the runtime state file — are excluded by
name, so the engine's own bookkeeping can never be mistaken for the agent's
work.

Before 0.6.5 each runner compared `git status` against a snapshot taken once
when the loop was wired and never refreshed. From lap 2 onward any write by
any earlier lap made every later lap look busy, so `changed` could not report
`False` and this soft bound **could not fire at all** for any runner that
shells out. Six runners each carried their own copy of that detector, three
of them annotated as mirrored-not-imported. The digest is now a single shared
function, and git is no longer required by the engine for anything.

**Reading utilisation off the receipt.** Every ledger row carries
`attempted`, which is `false` only for the two checks the controller performs
*before* a turn — the kill switch and the budget ceiling. Those record a lap
on which the worker was never invoked. Without the flag, a lap count and an
attempt count are indistinguishable: a ceiling halt at `max_iterations: 10`
writes an eleventh row, and anyone computing consumed-over-declared from the
ledger gets 1.1 for a bound that in fact held exactly. A cost claim that
cannot be audited from its own receipt is not a cost claim.

A wallclock halt keeps `attempted: true` — the worker did run, and ran out of
budget, which is the opposite of never having started.

## 2. Sandboxing — isolated scratch copy, symlinks refused

**Field:** `bounds.sandbox` (default `true`). **Enforced by:**
`composition._make_scratch_workspace()`, called from `wire()` before any
runner is invoked.

Why it matters: the loop's `seed/` is the source of truth checked into the
repo. If the agent operated on it directly, every run would mutate the
loop's own fixture — the next `bl run` would start from wherever the last
run left off, and a community-contributed loop's `seed/` could never be
trusted to reproduce. The engine instead copies `seed/` into a fresh
`tempfile.mkdtemp()` directory per run. The function also refuses to run if
`seed/` itself, or anything inside it, is a symlink — a malicious loop
could otherwise ship `seed -> ~/.ssh` and have `copytree` follow it,
defeating the sandbox before the copy even happens.

## 3. Input quarantine — the "governed workspace" guarantee

**Field:** `bounds.quarantine_inputs` (default `true`). **Enforced by:** the
`ignore=_quarantine_ignore` callback passed to `shutil.copytree` inside
`_make_scratch_workspace()`.

Why it matters: bounded-loops explicitly invites community loop PRs, which
means a loop's `seed/` is not fully trusted. Without this bound, a
malicious loop could plant a reader for `.env`/`.ssh`/`.aws`/`id_rsa`-style
files, or a careless one could ship a real credential that then reaches an
agent running inside the sandbox. Quarantine excludes secret-bearing paths
by name (`.git`, `.env*`, `.ssh`, `.aws`, `.gnupg`, `.netrc`, `credentials`,
`id_rsa`/`id_dsa`/`id_ecdsa`/`id_ed25519`) and by suffix (`.pem`, `.key`,
`.p12`, `.pfx`) at every directory level of the copy, case-insensitively.
Set `false` only for a loop that legitimately needs such a file present
(e.g. a secret-scanning demo with a deliberately fake key).

## 4. Output schema validation

**Field:** `bounds.schema` (a path string, or `null`). **Enforced by:**
`JsonSchemaGate`, which a loop opts into via `gate.kind: jsonschema` in
`loop.yaml`; `composition._instantiate_gate()` wires the schema path from
`manifest.bounds.schema`.

Why it matters: for loops whose "done" condition is producing correctly
shaped structured output (not passing a test suite), a JSON Schema is a
mechanical, dependency-light way to prove shape and type correctness
without writing a bespoke checker. `JsonSchemaGate` treats a missing or
non-conforming `output.json` as a normal `Verdict(passed=False)`, never an
exception — only a missing/malformed *schema file itself* raises
`GateError` at construction time.

## 5. Tracing — one OTel span per lap

**Field:** `bounds.trace` (default `true`). **Enforced by:** the
`TracerPort` — `OtelTracer` when both `bounds.trace` is true and OTel is
requested via the `BOUNDED_LOOPS_OTEL` env var (checked in
`composition._otel_requested()`), otherwise `NoopTracer` by default, which
keeps the repo dependency-free and keyless out of the box.

Why it matters: loop engineering that can't be observed can't be debugged
or trusted in production — a span per lap is the minimum telemetry needed
to answer "how many laps did this take, and where did the time go" without
grepping a ledger file by hand. Defaulting to a no-op tracer means this
bound costs nothing for the keyless quick-start.

## 6. Regression evaluation — satisfied by the gate choice, not a `Bounds` field

**Field:** none — this bound has no `bounds.yaml` key. **Enforced by:**
whichever `GatePort` adapter the loop selects via `gate.kind` in
`loop.yaml` (`command`, `pytest`, `jsonschema`, `osv`, `checkov`).

Why it matters: "did this change actually fix the thing, without breaking
something else" is inherently gate-specific — a legal-citation checker and
a vulnerability scanner have nothing in common mechanically. Rather than
force a generic regression-eval boolean that would mean nothing across
domains, the project makes the gate itself the unit of "is this a real,
independent regression check" — every shipped gate is a real tool
(pytest, a JSON Schema, an OSV/Checkov scan, or any exit-code-checked
command), never "an LLM decides."

## 7. Token budget

**Field:** `bounds.max_tokens` (an int, or `null` for no cap). **Enforced
by:** `BudgetMeterPort.exceeded()` (concretely `BudgetMeter` in
`adapters/io/budget.py`), checked at the top of every lap, fed by
`BudgetMeter.spend(result.tokens)` called immediately after each runner
turn.

Why it matters: an agent that never stops but also never crashes is a
billing incident waiting to happen. Token accounting is only as honest as
its source, though: `claude-code` parses real `total_cost_usd`/`usage`
tokens from the CLI's own JSON output; `shell`/`codex`/`antigravity`
report `0` tokens today — the README calls this an honest tool limitation,
not a silently-swallowed gap — while `stub`/`python_callable` supply
whatever count the cassette or glue code provides.

## 8. Human approval gating

**Field:** `bounds.require_approval` (default `null`, meaning "derive from
rung"). **Enforced by:** `domain.rules.rung_requires_approval()` (pure
predicate), invoked in `run_loop.py` right after a gate passes, gating
whether `ApprovalPort.granted()` must return `True` before a DONE outcome
is returned. Composition wires `CliApproval` (prompts stdout/stdin) when
approval is required, `AutoApproval` (always returns `True`) otherwise.

Why it matters: passing the gate is necessary but should not always be
sufficient to let an agent's work ship unattended. The rung ladder gives a
sane default — L1 (report) never needs approval because a human is reading
every verdict anyway; L2 (assisted) and L3 (unattended) require it by
default, because "the mechanical gate agreed" and "a human is comfortable
merging this" are different bars. `bounds.require_approval` can override
the rung-derived default explicitly in either direction.

## 9. Wall-clock timeout — enforced inside an attempt, not only between them

**Field:** `bounds.max_wallclock_s` (an int, or `null`). **Enforced by:**
`BudgetMeterPort.exceeded()` before each lap begins, *and*
`BudgetMeterPort.wallclock_budget()` inside each attempt: the controller
measures the remaining budget immediately before handing off to the runner,
and the runner clamps its own wait to it
(`adapters/runners/attempt_deadline.py`). No attempt starts after the
ceiling and none continues past it.

Why it matters: `manifest.py`'s `_load_bounds()` is explicit that
`max_wallclock_s: null` in `bounds.yaml` does **not** mean "run forever" —
it is normalized to a conservative 3600-second (1-hour) default at
manifest-load time. This is a deliberate security fix: an unbounded
`max_iterations` (already capped at 1000) combined with a truly unbounded
wallclock and token budget would still describe an effectively-unlimited-
cost loop. A loop that genuinely needs longer than an hour must say so
explicitly, not get it by omission.

### What changed in 0.6.5, and what the bound now actually promises

Until 0.6.5 this ceiling was compared against elapsed time **only at the top
of a lap**, so it bounded the gap between attempts rather than an attempt. A
loop declaring `max_wallclock_s: 120` was observed running a single attempt
for over 300 seconds, terminated in the end by a runner default it had never
declared. The number was in the manifest and readable; it just did not
constrain anything an operator would recognise as the run.

The promise now, stated exactly:

> Worker time is bounded by `max_wallclock_s`. Total run time is bounded by
> `max_wallclock_s` **plus at most one gate timeout**.

That second clause is deliberate and is not a gap left open by accident.
Gates are **not** clamped to the remaining budget: cutting a gate off
mid-check yields no verdict, and a check that could not run must never be
recorded as having judged. A verdict that cost a few extra seconds is worth
strictly more than no verdict at all.

### Two limits on one attempt, and which one bit

| Limit | Whose decision | Exceeding it means | Status |
|---|---|---|---|
| `bounds.max_wallclock_s` | the loop author's, declared in the manifest | the declared spend ceiling was reached | `HALT`, reason names the bound |
| the runner's `timeout_s` | the operator's, set on the adapter | one turn ran longer than this deployment tolerates | `ERROR` |

The tighter limit binds, and the receipt says which. They are reported
differently on purpose: a run that stopped because it was told to is not a
run that broke, and filing both under one heading is how a budget ceiling
gets mistaken for a crash.

The agent-CLI runners default to a 600-second per-turn timeout. That number
is measured, not guessed: across four providers driving four loops,
completed turns ran to 178 seconds and one was still working when a previous
300-second default killed it, so 300 was truncating work that was still
progressing.

### Sizing the ceiling for a real provider

The shipped catalogue uses **`max_wallclock_s = max_iterations × 90`**.
The 90 seconds per attempt comes from the same measurement (median turn
44.6s, 75th percentile 72.5s), and sits deliberately *below* the slowest
turn observed. That is a choice: a run whose every turn is as slow as the
worst one measured will halt on budget before reaching its lap cap, which is
what a spend limit is for. Sizing it to the slowest turn instead would make
the ceiling arithmetically incapable of firing — present in the manifest and
unable to affect a run, which is the defect this release closes.

`tests/loops/test_wallclock_ceilings_fit_a_real_agent.py` enforces the rule
across the catalogue and refuses to let the per-attempt allowance be raised
past the slowest measured turn.

### The handoff reserve — a bound need not destroy the work it interrupts

**Field:** `bounds.handoff_reserve_s` (an int; default 90, `0` declines).

A hard bound has to be hard. But one that reports only "budget exceeded"
throws away spend already paid for — and worse, the next run starts from the
same seed with the same budget and no knowledge of what the last one learned.
A task that genuinely needs more than one budget window can then never
finish, however many times it is run. The bound stops being merely strict and
becomes anti-productive.

So a bound halt now leaves two things behind:

1. **`HANDOFF.md`, written by the harness**, beside the ledger. Which bound
   fired, attempts spent, which laps changed the workspace, the gate's last
   message, and — the question a reader actually has — whether the run was
   *stuck* or *short of budget*. Costs nothing, always available, cannot be
   wrong about what happened.
2. **One wind-down turn, written by the agent**, if the reserve allows. This
   is the part that can say *"I was part-way through record 6 and the checksum
   field is the problem"*. It is marked **unverified** in the document, because
   no gate has looked at it.

**The reserve is taken OUT of `max_wallclock_s`, never added to it.** Work
gets `ceiling − reserve`; the wind-down gets the reserve; the declared total
is unchanged. That is precisely what keeps the termination guarantees intact —
there is no branch on which a run outlives its declared ceiling in exchange
for a summary. Granting the turn *after* the ceiling would have made the
ceiling silently mean "`max_wallclock_s` plus however long a summary takes",
and that second term is exactly the quantity an operator cannot see. It is the
same defect as a declared-but-unenforced bound, arrived at from the opposite
direction.

Consequences worth knowing:

- **The kill switch gets no wind-down.** An operator pulling it wants the run
  to stop now, not to spend more of anything. Every *other* bound halt gets
  one — no-progress included, since a stuck agent's account of what it tried
  is the most useful handoff of the set, not the least.
- **Nothing in the wind-down can change the terminal status.** A handoff turn
  that hangs, crashes or writes nonsense costs the reserve and no more.
  Otherwise "we tried to help you" could turn a clean HALT into a failure.
- `manifest.py` **refuses** an authored reserve at or past half the ceiling —
  an author who wrote the number should be told it is wrong. A `Bounds`
  assembled in code instead **clamps** to half the ceiling, so the default
  cannot break a previously-valid construction. Loud for input, forgiving for
  internals.
- The wind-down turn's tokens are metered like any other. The budget is
  already spent; not counting them would understate what the run cost.

## The kill switch — highest priority, polled first

**Env var:** `BOUNDED_LOOPS_KILL` (any non-empty value trips it).
**Enforced by:** `EnvKillSwitch.tripped()`, polled by `RunLoopUseCase.run()`
at the very top of every lap — before the budget check, before the runner
is ever invoked.

Why it matters: every other bound is evaluated by engine logic reasoning
about the loop's own state (laps, tokens, wallclock, progress). The kill
switch is the one bound designed for a human or supervisor process to pull
externally, on demand, for any reason at all — and it's deliberately an
environment variable rather than a workspace-local file, because a file
inside the scratch workspace sits in the same trust boundary as
agent-writable content; an untrusted agent could touch or delete a `.kill`
sentinel it can see, but it cannot reach into its own supervisor's
environment.

## Where each bound sits across the layers

![Where each bound sits across the layers: bounds.yaml fields flow through manifest.py validation into either composition.py wiring or the run_loop.py engine that enforces them](diagrams/bounds-across-layers.svg)

> Editable source: [`diagrams/bounds-across-layers.mmd`](diagrams/bounds-across-layers.mmd) · regenerate with `mmdc -i diagrams/bounds-across-layers.mmd -o diagrams/bounds-across-layers.svg`

## The graph engine, node by node

The bounded-loops graph engine (`bl graph`) composes a DAG of nodes, each
gated independently of the worker that produced its output (worker≠gate,
controller-enforced) — the same non-negotiable behind every bound on this
page, now enforced once per node rather than once per loop. Graph nodes
declare their own per-node controls (budget, effects, isolation) rather
than this file's `bounds.yaml` fields, but the underlying principle is
identical: no worker grades its own work. See
[graph-capabilities.md](./graph-capabilities.md) for the node-level
capability and isolation reference.

## See also

- [ARCHITECTURE.md](./ARCHITECTURE.md) — the hexagonal design these bounds
  are wired into.
- [WRITING-A-LOOP.md](./WRITING-A-LOOP.md) — how a new loop configures these
  fields in practice.
