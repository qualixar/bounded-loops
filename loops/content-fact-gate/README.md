# content-fact-gate

**Pattern:** evaluator-optimizer · **Role:** content, writing · **Rung:** L2 · **Gate:** command (markdown-link-check)

Demonstrates a `command` gate wrapping [markdown-link-check](https://github.com/tcort/markdown-link-check):
drive an agent until every citation link in a piece of content resolves,
catching dead/hallucinated links before publish. Same "verify before you
ship" discipline this repo's other loops demonstrate for a failing test
(`bug-fix-red-green`) and a vulnerable dependency (`osv-scanner-example`),
applied here to written content.

## What happens

`seed/article.md` ships with two citation links: one pointing at
`https://this-domain-absolutely-does-not-exist-boundedloops-demo.invalid/some-page`
(the `.invalid` TLD is IANA-reserved by RFC 2606 — guaranteed to never
resolve, so the "dead link" here is deterministic, not flaky), and one
pointing at a real, stable source, `https://www.iana.org/domains/reserved`.
The loop runs an agent against `PROMPT.md`, checks
`npx --yes markdown-link-check seed/article.md` after each lap, and halts
as soon as the gate is clean.

## Prerequisites (one-time)

```bash
# Node.js + npm — install once, not part of the timed run below.
node -v   # verified against v22.22.3
npm -v    # verified against 10.9.8
```

No global install is required. `npx --yes markdown-link-check` fetches and
runs the tool on demand (verified working here without a prior
`npm install -g`). If your environment can't reach the npm registry at run
time, fall back to `npm install -g markdown-link-check` once, then drop
`npx --yes` from `loop.yaml`'s `gate.run`.

## Prove the gate genuinely fails on the unfixed seed

```bash
cd loops/content-fact-gate
npx --yes markdown-link-check seed/article.md
echo "exit code: $?"
```

Real captured output:
```
FILE: seed/article.md
  [✖] https://this-domain-absolutely-does-not-exist-boundedloops-demo.invalid/some-page
  [✓] https://www.iana.org/domains/reserved

  2 links checked.

  ERROR: 1 dead link found!
  [✖] https://this-domain-absolutely-does-not-exist-boundedloops-demo.invalid/some-page → Status: 0

exit code: 1
```

## Run it with the engine

```bash
# from repo root
pip install -e .
bl lint loops/content-fact-gate
bl run loops/content-fact-gate --yes
```

Real captured output:
```
[bounded-loops] About to run loop 'content-fact-gate':
  runner : stub
  gate   : npx --yes markdown-link-check seed/article.md
✓ [DONE] gate-passed (laps: 1)  ledger: loops/content-fact-gate/.ledger.jsonl
```

Lap 1's cassette rewrites `seed/article.md`, replacing the dead `.invalid`
citation with the same live IANA reference the article already cites
elsewhere — both sentences are genuinely about IANA-administered
domain-name policy, so this is a real citation fix, not just link-swapping.
`markdown-link-check` then reports all links alive (exit 0), and the loop
reaches DONE.

## Known limitation

Unlike every other gate in this repo (pytest, osv-scanner, checkov — all
fully offline and deterministic against a local file), this gate makes
**real network calls**: `markdown-link-check` actually resolves each URL
over HTTP(S). That means a run's result can be affected by network
availability, DNS resolution behavior, or the target site's uptime — not
just the content of `article.md`. The seed intentionally uses an
IANA-reserved `.invalid` domain for the "dead" link (RFC 2606 guarantees it
never resolves, so that half of the check is deterministic) and a stable
IANA reference page for the "alive" link, to keep the demo as reliable as
a network-calling gate can be — but it is not offline-hermetic the way the
other loops' gates are.

Production mitigation: run this gate from CI or a controlled network where DNS
and outbound HTTPS behavior are predictable, pin the link-checker version in
your project, and keep the article itself as the only mutable artifact. For
offline release gates, replace live URL checks with a checked-in citation index
or an internal content API export and gate against that deterministic source.

## Lift it into your own repo

1. Copy this folder.
2. Replace `seed/article.md` with your own content and citation links.
3. Edit `PROMPT.md` to describe your goal.
4. Run `bl run loops/<your-copy> --yes` to prove it works.

## Which Anthropic pattern

`evaluator-optimizer` — the gate (markdown-link-check) is the evaluator;
the agent-turn is the optimizer. The loop runs until the evaluator says
every link is alive.
Reference: [Anthropic — Building effective agents](https://www.anthropic.com/research/building-effective-agents)
