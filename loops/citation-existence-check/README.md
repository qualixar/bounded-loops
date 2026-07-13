# citation-existence-check

**Role:** legal · research · **Rung:** L2 · **Gate:** `command` (keyless) · **Runner:** stub (keyless)

A bounded loop that drives an agent until **every case citation in a legal
document resolves to a real case** in a trusted reporter. This is the runnable
form of the single most-documented AI failure in law: fabricated or mis-cited
authorities — over 1,600 sanction cases catalogued in the Charlotin "AI
Hallucination Cases" database as of mid-2026.

## What it demonstrates

The seed `brief.md` cites five authorities. Two are wrong:

- **Miranda v. Arizona, 384 U.S. 999** — real case, fabricated page (it's `436`).
- **Thompson v. Halden, 599 U.S. 1201** — does not exist at all.

The gate `seed/check_citations.py` derives the set of valid reporters from
`known_reporter.json` and flags any `VOLUME REPORTER PAGE` citation that is not
a real case. The loop is DONE only when the checker exits 0.

## Run it (keyless, ~1s)

```bash
bl run loops/citation-existence-check --yes   # stub runner + real command gate
```

You'll see lap 1 correct Miranda's page while leaving the fabricated Thompson
cite. The agent claims done, but the gate fails. Lap 2 removes Thompson and the
same gate passes. The two-lap receipt shows why agent self-reporting is not an
acceptance criterion.

## Why the reporter and checker are `forbid:`-protected

The whole point is that the agent conforms the *document* to reality. Letting it
"fix" the failure by adding `Thompson v. Halden` to the reporter, or by editing
the checker, would fake a green gate — exactly the "agent talks its way past the
verifier" failure bounded-loops exists to prevent. The engine refuses any write
to `seed/check_citations.py` or `seed/known_reporter.json`.

## Make it real

Swap the stub runner for a real agent and point `known_reporter.json` at (or
have the checker query) a real reporter — CourtListener, a Westlaw/Lexis export,
or your firm's citation database. Keep the gate as the bottleneck: a brief is
never "done" until every cite is independently verified to exist.
