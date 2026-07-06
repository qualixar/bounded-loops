# osv-scanner-example

**Pattern:** evaluator-optimizer · **Role:** security, engineering · **Rung:** L2 · **Gate:** osv

Demonstrates the `OsvGate`: drive an agent until Google's
[osv-scanner](https://github.com/google/osv-scanner) reports zero known
vulnerabilities in the workspace's dependency lockfile.

## What happens

`seed/package-lock.json` pins `minimatch@3.0.4` — a real, disclosed-vulnerable
version (4 CVEs: GHSA-23c5-xmqv-rm74, GHSA-3ppc-4f35-3m26, GHSA-7r86-cg39-jmmj,
GHSA-f8q6-p94x-37v3; confirmed live against `osv-scanner` 2.4.0, not guessed).
The loop runs an agent against `PROMPT.md`, checks `osv-scanner scan --format
json --recursive .` after each lap, and halts as soon as the gate is clean.

## Prerequisites (one-time)

```bash
# osv-scanner — install once, not part of the timed run below.
brew install osv-scanner
```

## Prove the gate genuinely fails on the unfixed seed

```bash
cd loops/osv-scanner-example
osv-scanner scan --format json --recursive seed/
echo "exit code: $?"   # 1 — real vulnerabilities found
```

Real captured output (trimmed) confirms `minimatch@3.0.4` and 4 vulnerability
IDs, exactly matching `OsvGate._summarize`'s assumed shape.

## Run it with the engine

```bash
# from repo root
pip install -e .
bl lint loops/osv-scanner-example
bl run loops/osv-scanner-example --yes
```

Expected:
```
status: DONE  laps: 1  ledger: loops/osv-scanner-example/.ledger.jsonl
```

Lap 1's cassette bumps the `minimatch` pin from `3.0.4` to `3.1.4` (the real
fixed version for the 0.x–3.x advisory range) — `osv-scanner` then reports
zero known vulnerabilities (exit 0), and the loop reaches DONE.

## Lift it into your own repo

1. Copy this folder.
2. Replace `seed/package-lock.json` (or any osv-scanner-recognized manifest —
   `go.mod`, `Cargo.lock`, `requirements.txt`, etc.) with your target.
3. Edit `PROMPT.md` to describe your goal.
4. Run `bl run loops/<your-copy>` to prove it works.

## Which Anthropic pattern

`evaluator-optimizer` — the gate (osv-scanner) is the evaluator; the
agent-turn is the optimizer. The loop runs until the evaluator says clean.
Reference: [Anthropic — Building effective agents](https://www.anthropic.com/research/building-effective-agents)
