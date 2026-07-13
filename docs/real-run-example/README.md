# Verified Codex run

This receipt comes from a real bounded-loops run on 2026-07-13 with
`codex-cli 0.144.3`, using the logged-in CLI's default model:

```bash
bl run loops/citation-existence-check \
  --runner codex \
  --yes \
  --run-id codex-0.3.0-verified \
  --keep-workspace
```

The source brief began with two invalid citations. Codex corrected Miranda's
page and removed the fabricated Thompson authority in one turn. It did not edit
the protected reporter or checker. The independent command gate then exited 0:

```text
check_citations: every citation resolves to a real case in the reporter
✓ [DONE] gate-passed (laps: 1)
Gate verified: the independent acceptance gate passed after 1 lap.
```

[`ledger.jsonl`](ledger.jsonl) is the engine's persisted ledger entry. It
records the verdict, decision, lap count, wall-clock spend, and the 228,149
input/output tokens reported by Codex. [`transcript.jsonl`](transcript.jsonl) is
a machine-readable excerpt containing the task-relevant events and exact usage
object.

The raw Codex event stream is intentionally not committed. Codex loaded global
skills and memory available on the verification machine, so the raw stream
contains unrelated local paths and context. The excerpt removes those events;
it does not alter the completion usage or gate result.

The deterministic cassette takes two laps on purpose so users can see a failed
verdict before convergence. The real Codex agent fixed both findings in its
first lap. Both paths still terminate only after the same independent gate
passes.
