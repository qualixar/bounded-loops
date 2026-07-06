# nda-required-clauses

**Role:** legal · **Rung:** L2 · **Gate:** `command` (keyless) · **Runner:** stub (keyless)

A bounded loop that drives an agent until **a mutual NDA contains every
clause a working NDA needs**. This is the runnable form of a common
drafting defect: an agreement assembled from a partial template that
protects confidentiality but never says what forum resolves disputes,
or never requires materials to be returned or destroyed when the
relationship ends.

## What it demonstrates

The seed `nda.md` has five clauses but is missing two:

- **Governing Law** — no jurisdiction is named for disputes under the
  Agreement.
- **Return of Materials** — no obligation to return or destroy
  Confidential Information at the end of the engagement.

The gate `seed/check_clauses.py` scans the document for five required
clauses (Confidentiality, Term/Duration, Governing Law, Return of
Materials, Permitted Disclosures) by case-insensitive keyword/heading
match. The loop is DONE only when all five are present.

## Run it (keyless, ~1s)

```bash
bl run loops/nda-required-clauses --yes   # stub runner + real command gate
```

You'll see the incomplete NDA fail the checker, the recorded fix add the
two missing sections, then the gate pass.

## Why the checker is `forbid`-protected

The whole point is that the agent conforms the *document* to the
requirement, not the requirement to the document. Letting it "fix" the
failure by editing the checker to drop a required clause would fake a
green gate — exactly the "agent talks its way past the verifier"
failure bounded-loops exists to prevent. The engine refuses any write to
`seed/check_clauses.py`.

## Make it real

Swap the stub runner for a real agent and extend `REQUIRED_CLAUSES` in
the checker to match your firm's NDA clause checklist, or point it at a
clause-classification model. Keep the gate as the bottleneck: an NDA is
never "done" until every required clause is independently confirmed
present.
