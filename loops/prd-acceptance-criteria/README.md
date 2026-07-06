# prd-acceptance-criteria

**Role:** business · **Rung:** L1 · **Gate:** `command` (keyless) · **Runner:** stub (keyless)

A bounded loop that drives an agent until **every user story in a PRD has
real, testable acceptance criteria**. This is the runnable form of a very
common PM failure: a story ships to engineering with a nice narrative and
no definition of done, so QA has nothing to verify against and "done"
becomes a debate.

## What it demonstrates

The seed `prd.md` has three stories. One is missing acceptance criteria
entirely:

- **"As a customer, I want a receipt emailed to me"** — no `### Acceptance
  Criteria` subsection at all.

The gate `seed/check_prd.py` splits the document by `## Story:` headings
and flags any story whose body doesn't contain an `### Acceptance Criteria`
subsection with at least one bullet or checkbox. The loop is DONE only when
the checker exits 0.

## Run it (keyless, ~1s)

```bash
bl run loops/prd-acceptance-criteria --yes   # stub runner + real command gate
```

You'll see the incomplete story fail the checker, the recorded fix add
concrete acceptance criteria, then the gate pass.

## Why the checker is `forbid:`-protected

The whole point is that the agent conforms the *PRD* to a real, testable
standard. Letting it "fix" the failure by editing the checker to stop
requiring acceptance criteria would fake a green gate — exactly the "agent
talks its way past the verifier" failure bounded-loops exists to prevent.
The engine refuses any write to `seed/check_prd.py`.

## Make it real

Swap the stub runner for a real agent and point this at your actual PRD
tool export (Linear, Jira, Notion-to-markdown, or a raw doc). Keep the gate
as the bottleneck: a story is never "done" being written until QA has
something concrete to test.
