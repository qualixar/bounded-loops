# rfc-decision-recorded

**Role:** business · **Rung:** L1 · **Gate:** `command` (keyless) · **Runner:** stub (keyless)

A bounded loop that drives an agent until **an RFC document actually
records a decision**: Status, Context, Decision, and Consequences all
present. This is the runnable form of a very common engineering-org
failure: an RFC marked "Accepted" that lays out the problem and the options
considered, but never states which option won or what it costs — useless
to anyone reading it six months later trying to understand why the system
looks the way it does.

## What it demonstrates

The seed `rfc.md` is marked `Status: Accepted` and has a solid `Context`
section listing three options, but stops there:

- **No `Decision` section** — which option was actually chosen is never
  stated.
- **No `Consequences` section** — the tradeoffs of that choice are never
  written down.

The gate `seed/check_rfc.py` scans the document's headings (case-insensitive)
and flags any of Status/Context/Decision/Consequences that's missing. The
loop is DONE only when the checker exits 0.

## Run it (keyless, ~1s)

```bash
bl run loops/rfc-decision-recorded --yes   # stub runner + real command gate
```

You'll see the incomplete RFC fail the checker, the recorded fix add a real
Decision and Consequences section consistent with the existing Context,
then the gate pass.

## Why the checker is `forbid:`-protected

The whole point is that the agent conforms the *RFC* to a real
decision-record standard. Letting it "fix" the failure by editing the
checker to stop requiring a Decision section would fake a green gate —
exactly the "agent talks its way past the verifier" failure bounded-loops
exists to prevent. The engine refuses any write to `seed/check_rfc.py`.

## Make it real

Swap the stub runner for a real agent and point this at your actual RFC
repo (a `docs/rfcs/` folder, Confluence export, or ADR directory). Keep the
gate as the bottleneck: an RFC is never "Accepted" until the decision and
its consequences are actually written down, not just implied.
