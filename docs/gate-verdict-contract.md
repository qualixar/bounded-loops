# The gate verdict contract

A gate answers one of three things, and the difference decides what the harness does next.

| verdict | exit | meaning | what the loop does |
|---|---|---|---|
| **pass** | 0 | the work satisfies the requirement | stop, converged |
| **reject** | 1 | the gate read the work and the answer is no | hand the reason back and retry |
| **error** | 2 | the gate could not form an opinion at all | halt the run for a human |

The line that matters is between **reject** and **error**, and it is not "was the input what I
hoped for". It is:

> **Is this the worker's fault?**
>
> Anything wrong with an artifact the worker OWNS is a **reject**. That includes missing, empty,
> whitespace, unparseable as its declared format, and structurally vacuous.
>
> **Error** is reserved for the gate itself being unable to run: a bad argv, an internal crash, or
> a file the LOOP ships that the worker never had permission to touch.

The manifest already states which is which. A path in `forbid` is loop-shipped — the worker cannot
edit it, so its absence or corruption is not the worker's failure and retrying cannot fix it.
Everything else in the workspace is the worker's output.

```yaml
# loops/dataset-license-allowed/loop.yaml
gate:
  run: python3 seed/check_licenses.py seed/datasets.json seed/allowlist.json
forbid: [seed/check_licenses.py, seed/allowlist.json]
#                                ^ loop-shipped: unreadable => ERROR (exit 2)
#         seed/datasets.json is absent from forbid
#                                ^ worker-owned: unreadable => REJECT (exit 1)
```

## Why this is the whole point of a harness

An *agent harness* is the code wrapping a model in an execution loop: it hands over a task, gives
tools, captures the result, checks whether it satisfies the goal, and decides whether to retry or
stop. The term was formalised in early 2026 around six components — task definition, context
management, tool execution, **loop control**, **verification**, and **failure handling**. The last
two are what this file governs, and getting them wrong makes the other four irrelevant.

The convergent industry answer is that a failure caused by the work must be **returned to the
worker**, never thrown past it:

* **Anthropic** — return errors as tool results rather than raising through the agentic loop,
  because the model needs to see the error to adapt; a swallowed exception becomes silent failure
  or hallucinated success. Error text is prompt-engineered to be actionable rather than an opaque
  code.
* **AWS Step Functions / Azure Durable Functions** — separate *transient* faults, which retry, from
  *permanent* ones like `InvalidUserDataException`, which retrying cannot fix and which must be
  reported rather than crashed on.
* **Terraform** — `plan -detailed-exitcode` returns three codes instead of two, and that separation
  is treated as the foundation of a serious CI pipeline. Two codes cannot express "ran, and the
  answer is no" distinctly from "did not run".

Bounded loops is a Plan-Execute-Verify harness whose verification comes from deterministic sensors —
parsers, type checkers, test suites, schema validators. This contract is the failure-handling half.

## The defect this contract was written to close

Twenty-four shipped checkers returned exit **2** for an artifact that was empty, vacuous, or
malformed. `CommandGate` classifies 2 as a gate ERROR, so:

* **In production**, a worker that wrote broken JSON halted the run instead of being told to fix it.
  The loop had a correct, specific diagnosis in hand and threw it away.
* **In the mutant corpus**, 84 of 233 mutants were recorded as "not judged" and left α entirely.
  Seven loops produced nothing but errors while the reported rate looked perfect.

The vacuity was *detected*. It was the filing that was wrong — which is the hardest version of this
bug to see, because the guard is present and reads correctly. Only running it end to end shows the
answer never reaches a caller.

## Writing a new gate

1. Read the artifact. Cannot read a **worker-owned** file → **1**, saying what was expected.
2. Parse it. Does not parse → **1**. The worker wrote it.
3. Check for the subjects of the requirement. None → **1**. "Nothing to check" is an answer, and it
   is the answer that a vacuous artifact is designed to extract from a careless gate.
4. Evaluate. Violations → **1**. Clean → **0**.
5. Reserve **2** for: wrong argv, a `forbid`-listed input that is missing or corrupt, an unexpected
   internal exception.

Every message on the **1** path is read by an agent that will try again. Write it as an instruction,
not a stack trace.
