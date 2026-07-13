# a11y

**Pattern:** evaluator-optimizer · **Role:** frontend, engineering · **Rung:** L2 · **Gate:** command (`a11y_lint.py`)

Drive an agent until a **static accessibility linter reports zero
violations** in an HTML file. The gate is `seed/a11y_lint.py` — a tiny,
dependency-free checker shipped *with* the loop, written against the Python
standard library only. **No browser, no WebDriver, no npm, no external
tool, no API key.** It runs anywhere Python does — which is the whole point:
a genuinely keyless, clone-and-run accessibility gate.

## Why a shipped static linter, not axe-core (or eslint)

`axe-core` and Lighthouse inspect the *rendered DOM* — they need a running
browser, a WebDriver session, and a live URL. That does not fit
bounded-loops' keyless, static, CLI-only design. And when we went looking,
**no keyless *universal* accessibility tool exists** — the good ones all
need a browser (axe/pa11y/Lighthouse) or a running Java VM
(`html5validator`/Nu). Rather than fake a heavy dependency, this loop ships
a small, honest linter that checks the high-value, statically-detectable
WCAG subset with zero setup.

**This is a real, honest limitation, stated plainly:** a static linter
catches source-level a11y smells. It does NOT catch rendered-DOM issues —
computed colour contrast, real focus order, ARIA state that only exists
after JavaScript runs, reading order. For those you still need a DOM-based
tool against a live page. Treat this loop as a fast, keyless, static first
line of defense — not a replacement for a full audit before shipping.

The three rules it enforces (each one a browser-based tool would *also*
flag, but that you can catch at author time with zero setup):

| Rule | Check |
|---|---|
| A1 | every `<img>` must have an `alt` attribute (empty `alt=""` is fine for decorative images; a *missing* alt is the violation) |
| A2 | the root `<html>` must declare a `lang` |
| A3 | every labelable `<input>` must have a matching `<label for=...>` or an `aria-label` |

## What happens

`seed/index.html` ships with all three violations: an `<img>` with no
`alt`, an `<html>` with no `lang`, and an unlabeled email `<input>`. The
loop runs an agent against `PROMPT.md`, checks `a11y_lint.py` after each
lap, and halts as soon as the gate is clean.

## Prerequisites

**None** beyond Python 3.11+ (which you already have — bounded-loops is
Python). No install step. That is the entire pitch of this loop.

## Prove the gate genuinely fails on the unfixed seed

```bash
cd loops/a11y
python3 seed/a11y_lint.py seed/index.html
```

Real captured output:

```
a11y_lint: 3 accessibility violation(s) in seed/index.html:
  - line 7: <img> is missing an `alt` attribute (A1)
  - <html> is missing a `lang` attribute (A2)
  - line 10: <input type='email'> has no associated <label for=...> and no aria-label (A3)
```

Exit code: 1. After the fix (add `lang="en"`, an `alt`, and a
`<label for="email">`), the same command prints `no static accessibility
violations` and exits 0.

## Run it with the engine

```bash
# from repo root
pip install -e .
bl lint loops/a11y
bl run loops/a11y --yes
```

Expected:
```
✓ [DONE] gate-passed (laps: 1)  ledger: .../loops/a11y/.ledger.jsonl
Gate verified: the independent acceptance gate passed after 1 lap.
```

Lap 1's cassette rewrites `seed/index.html` to fix all three violations —
`a11y_lint.py` then exits 0 and the loop reaches DONE, keyless, with no
external tool.

## Lift it into your own repo

1. Copy this folder.
2. Replace `seed/index.html` with your own HTML target (keep
   `seed/a11y_lint.py`, or extend it with more rules).
3. Edit `PROMPT.md` to describe your goal.
4. Run `bl lint loops/<your-copy>` then `bl run loops/<your-copy> --yes`.

## Which Anthropic pattern

`evaluator-optimizer` — the gate (`a11y_lint.py`) is the evaluator; the
agent-turn is the optimizer. The loop runs until the evaluator says clean.
Reference: [Anthropic — Building effective agents](https://www.anthropic.com/research/building-effective-agents)
