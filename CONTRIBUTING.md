# Contributing

The main way this repo grows is community-contributed loops, not a
hand-authored library maintained by one person. If you have a real gate
(a linter, scanner, test framework, schema check — something that
genuinely fails on bad input and genuinely passes on good input) and a
role/task it makes sense for, a new loop under `loops/` is a welcome PR.

## Adding a loop

1. Scaffold from the closest existing template:
   ```bash
   bl new --list                            # pytest-basic, jest-basic, go-test-basic,
                                            # cargo-test-basic, rspec-basic, junit-basic
   bl new pytest-basic loops/my-loop        # positional: <template> <destination>
   ```
   If none of the language templates fit (e.g. you're wiring a new gate
   kind, not a new test runner), copy the closest existing `loops/*`
   folder by hand instead — `bug-fix-red-green` for a `stub`-runner +
   `pytest`-gate loop, `osv-scanner-example` or `checkov-example` for a
   loop built around a real external scanner.

2. Every loop needs: `loop.yaml`, `bounds.yaml`, `PROMPT.md`, `README.md`,
   `STATE.md`, `seed/`, and `cassettes/default.json` (a deterministic,
   keyless cassette so `bl run --yes` works with zero API keys — this is
   what makes the loop provable in CI). Match an existing loop's file
   shape exactly rather than inventing a new layout.

3. **Prove your gate genuinely fails on the unfixed seed, then genuinely
   passes on the fixed one.** Run the real command yourself and paste
   the real output into your loop's README — don't write what you
   expect the output to look like. This project has caught real,
   shipped-broken assumptions this way more than once (a scanner's exit
   code meaning something different than the docs implied, a JSON shape
   varying based on input) — assume your first guess at a tool's
   behavior is wrong until you've actually run it.

4. Run the checks before opening a PR:
   ```bash
   bl lint loops/my-loop            # manifest + bounds validate
   bl run loops/my-loop --yes       # reaches status: DONE
   pytest -q                        # existing suite still green
   mypy bounded_loops               # if you touched any Python
   ruff check .
   ```

## Adding a gate or runner adapter

This is a bigger change than adding a loop — it touches
`bounded_loops/composition.py` (the only file allowed to import concrete
adapters), `bounded_loops/application/manifest.py` (manifest validation),
and `bounded_loops/cli.py` (CLI choices). Look at how the existing gates
(`bounded_loops/adapters/gates/`) and runners (`bounded_loops/adapters/
runners/`) are wired into all three files before starting — the pattern
is consistent across every one of them, and skipping any of the three
wiring points is the most common way a new adapter ships silently dead
(reachable in tests via direct construction, unreachable via `loop.yaml`
in practice). Every gate/runner classifies its subprocess outcomes into
exactly a normal pass, a normal fail, or a genuine launch/config error
(`GateError`/`RunnerError`) — never letting an unexpected exception
escape. New adapters should follow the same discipline: verify your
tool's real exit codes and output shape by actually running it, not by
trusting its documentation alone.

## Code style

No hardcoded secrets, no full-environment inheritance into any
subprocess (allowlist explicitly, matching the existing `_ENV_ALLOWLIST`
convention), explicit error classification over silent fallbacks. Small
helper duplication across adapter files (rather than a shared import) is
a deliberate convention here, not an oversight — it keeps each adapter
independently readable and avoids one file becoming a hidden dependency
of every other.

## Reporting a bug

Open an issue with the loop/command you ran and its real output. If it's
a security issue (a gate that can be tricked into a false pass, an env
var leaking somewhere it shouldn't), say so clearly in the title so it
gets triaged first.
