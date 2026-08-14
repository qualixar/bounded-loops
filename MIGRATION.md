# Migrating from 0.4.0 to 0.5.0

Most 0.4.0 setups need **no changes**. Three groups do:

| If you… | Read |
|---|---|
| implement `NodeWorkerPort` or `IndependentGatePort` | [1. The port signatures changed](#1-the-port-signatures-changed) — **required** |
| author graph manifests | [2. Graphs that used to validate may now be refused](#2-graphs-that-used-to-validate-may-now-be-refused) |
| parse `bl graph metrics` output | [4. The interval label changed](#4-the-interval-label-changed) |

Nothing about **loops** changed. `bl run`, `loop.yaml`, `bounds.yaml`, ledgers and the nine bounds
are unchanged, and every 0.4.0 loop package runs untouched.

---

## 1. The port signatures changed

**Breaking. Affects custom workers and gates only.** If you have never implemented one of these
Protocols, skip this section.

```python
# 0.4.0
def execute(self, *, plan, node, envelope, attempt: int) -> WorkerResult: ...
def evaluate(self, *, plan, node, result) -> GateVerdict: ...

# 0.5.0
def execute(self, *, plan, node, envelope, attempt: int, repair_round: int) -> WorkerResult: ...
def evaluate(self, *, plan, node, result, attempt: int, repair_round: int) -> GateVerdict: ...
```

Both new parameters are keyword-only and **required**. An unmigrated implementation raises
`TypeError: execute() got an unexpected keyword argument 'repair_round'` on the first attempt of the
first node. The migration is one line per implementation, and you may ignore the values:

```python
def execute(self, *, plan, node, envelope, attempt, repair_round) -> WorkerResult:
    ...  # unchanged body
```

### Why they are required rather than defaulted

A default would be a silent wrong answer. Attempts **reset** at a repair-round boundary, so
`(node, attempt=1)` happens once per round and `attempt` alone does not identify a unit of work. A
worker defaulting `repair_round=0` stamps round-3 work with a round-0 identity, and every receipt,
idempotency key and artifact provenance derived from it inherits that — inside a hash-chained log
that will then look perfectly consistent while describing work from a different round. A gate
defaulting it accepts evidence produced in another round.

If your worker or gate cares about identity at all, use both values. If it does not, accept and
discard them.

---

## 2. Graphs that used to validate may now be refused

0.4.0 accepted several declarations and then ignored them at runtime. They are now enforced, which
means a manifest that compiled before can fail validation now. **Every one of these errors names the
edge or node and what to write instead** — and in each case the declaration was never doing anything,
so a refusal is telling you your graph did not mean what it said.

### `when` conditions on edges

`when` now applies. It accepts only the source node's outcome: `succeeded`, `failed`, `skipped`,
`terminal`, or `null` (the default, meaning `succeeded`). Anything else — including a
data-dependent expression such as `result.status == 'failed'` — is refused instead of silently
dropped.

If a graph of yours stops compiling here, that condition was **never being applied** in 0.4.0: the
edge ran unconditionally.

### `when: failed | skipped | terminal` under `fail_mode: fail_closed`

Refused, because `fail_closed` stops the run at the first node failure, so such an edge could never
be reached. Use `fail_mode: continue_declared` if you want the branch, or drop the condition.

### `fail_mode: continue_declared` now does something

In 0.4.0 it was accepted and ignored — every run was fail-closed whatever the graph declared. If you
set `continue_declared` in 0.4.0 and relied on the run halting anyway, it will now continue past a
node's own bounded-loop failure. Continuation is deliberately narrow: a broken gate, a denied policy,
an isolation refusal, a missing worker, a rejected approval, an exhausted spend cap or an
unmeasurable budget still stop the run.

### Budgets and failure modes the runtime could not honour

A node with `max_attempts > 1` **and** a network effect is refused: an external or irreversible
effect cannot be re-driven without a per-effect idempotency key. This was already refused when the
controller was constructed; it is now caught at `bl graph lint` and in the studio too.

---

## 3. Run directories: what still resumes, and what does not

### Resumes normally

- **Every 0.4.0 run directory, including graphs with a `publish` node.** A 0.5.0-dev build briefly
  broke this by carrying `publication_policy` into the plan, which moved `plan_id` for exactly the
  graphs that have an irreversible effect. Fixed before release and pinned by a test that compiles a
  publish graph and compares against the `plan_id` v0.4.0's own compiler produced.
- Graphs with no repair declared: `repair_budget` and repair targets are omitted from the canonical
  form when unset, so their digests are byte-identical to 0.4.0.

### Diagnosing a mismatch

If a resume ever does report `Reconstructed plan_id … != stored …`, the message now tells you which
explanation applies. Run directories record `compiler_version`, so the error distinguishes "this
engine's compiler changed" from "this directory was modified" instead of leaving you to guess. A
directory written by 0.4.0 has no such key and the error says so rather than guessing.

### `fail_mode` is read from the manifest, not `run-meta.json`

`run-meta.json` is unsigned JSON that anyone with write access to the run directory can edit, while
`manifest.yaml` is covered by the graph digest. A disagreement between the two is now treated as
tampering and refused. A 0.4.0 directory with no recorded `fail_mode` still loads, taking the mode
from its manifest.

---

## 4. The interval label changed

`bl graph metrics` prints

```
emp-Bernstein 95% (COVERAGE-MEASURED)
```

where 0.4.0 printed `nominal-95% iid (UNCALIBRATED)`. If you parse that output, update the pattern.

The change is not cosmetic. The Wilson score interval assumed independent Bernoulli trials, which
retried attempts violate. It is replaced by an empirical-Bernstein interval whose coverage was
**measured** — 96.9% against Wilson's 77.5% under the simulated correlated-retry regime in
`tests/graph/application/test_confidence_sequence.py`.

Read the two limits before quoting either number:

- Both figures are coverage of the **per-run latent rate**, not of the marginal false-accept rate
  that `bl graph metrics` reports. Different estimands; the comparison says nothing about α coverage.
- The interval is **not** an anytime-valid confidence sequence. The radius is the fixed-time
  empirical-Bernstein form and carries no stitching term.

---

## 5. New in 0.5.0 — nothing to migrate, but you may want it

- **`kind: loop` nodes execute.** In 0.4.0 they compiled and linted, then were refused at preflight.
  A graph of yours containing one will now run real work where it previously stopped — worth knowing
  before you re-run an old manifest.
- **Six reference graphs** in [`graphs/`](graphs/), all keyless and runnable from a checkout.
- **`on_failure: {mode: repair, target: <ancestor>}`** with a global `policies.repair_budget`, now
  including on `kind: loop` nodes.
- **Loop input/output ports** for graph↔loop dataflow, declared in the loop's own `loop.yaml`.
- **`--loop-roots <dir>`** to add your own package catalog.

## Getting the loop catalog

`loops/` ships in the **repository**, not the wheel: `bl run` takes a path and writes its ledger
beside the loop, so a read-only copy inside `site-packages` would be the wrong shape. A pip install
gives you the engine; clone the repository for the 68-loop catalog and the reference graphs, or point
`--loop-roots` at your own.

```bash
pip install bounded-loops
git clone https://github.com/qualixar/bounded-loops && cd bounded-loops
bl run loops/bug-fix-red-green --yes
```
