# Writing a loop

This is a concrete how-to for authoring a new bounded loop, worked through
against the real, keyless, currently-running example:
[`loops/citation-existence-check/`](../loops/citation-existence-check/).

## The scaffold

```bash
bl new --list                    # pytest-basic, jest-basic, go-test-basic,
                                  # cargo-test-basic, rspec-basic, junit-basic
bl new pytest-basic my-loop       # <template> <destination> (positional)
```

`bl new` copies a packaged template tree to `<destination>`, strips the
`.tmpl` suffix from each file's final path component, substitutes
`{{LOOP_NAME}}` in every file's content, refuses to overwrite an existing
destination, and `chmod +x`'s whatever `.sh` files landed. A scaffolded (or
hand-written) loop folder has this shape — nine files, plus one directory:

| File | Role |
|---|---|
| `loop.yaml` | The manifest: name, description, `pattern` (one of Anthropic's seven agentic patterns), `role`, `rung`, pointers to `spec`/`bounds`/`memory`, the `runner.default`, the `gate.kind`(+`gate.run` for `command`/`pytest`), and `forbid:` globs. |
| `bounds.yaml` | The nine-bounds configuration for this loop — `max_iterations`, `no_progress_window`, `max_tokens`, `max_wallclock_s`, `sandbox`, `quarantine_inputs`, `schema`, `trace`, `require_approval`. |
| `PROMPT.md` | The spec text handed to the agent each lap — loaded verbatim as the single `Spec.steps` entry; the gate, not this file, is what actually proves the stop condition. |
| `STATE.md` | Cross-lap memory, read/written via `MemoryPort` — lives outside the agent-writable scratch workspace. |
| `seed/` | The broken starting state the agent operates on (copied into an isolated scratch dir at run time — never mutated in place). |
| `cassettes/default.json` | The stub runner's recorded cassette — see below. |
| `README.md` | Human-facing walkthrough: what the loop demonstrates, the "prove it fails then prove it passes" narrative. |
| `run.sh` / `wreck.sh` | Convenience wrappers: `run.sh` is the gated path; `wreck.sh` is the ungated contrast — same prompt, no independent gate, so it "wrecks" by confidently believing an unfixed bug is fixed. |

`citation-existence-check` follows this shape exactly (see
[`loop.yaml`](../loops/citation-existence-check/loop.yaml),
[`bounds.yaml`](../loops/citation-existence-check/bounds.yaml),
[`PROMPT.md`](../loops/citation-existence-check/PROMPT.md)) — its `seed/`
holds `brief.md` (the document to fix), `known_reporter.json` (the
read-only ground truth), and `check_citations.py` (the gate script itself,
shipped alongside the loop so the gate runs anywhere Python does, no
network or legal API required).

## The three keyless gate patterns

A gate is what decides whether a lap is DONE — never the agent's own
claim. Three patterns cover the large majority of loops without any paid
dependency:

**jsonschema** — for loops whose done-condition is "produce correctly
shaped structured output." Set `gate.kind: jsonschema` and
`bounds.schema: seed/output-schema.json` (or wherever the schema file
lives); `JsonSchemaGate` validates `workspace/output.json` against it.
Nothing beyond the core `jsonschema` dependency is needed — no network, no
key.

**command + stdlib checker** — for loops whose done-condition is "some
existing or purpose-built script exits 0." This is what
`citation-existence-check` uses:

```yaml
gate:
  kind: command
  run: "python3 seed/check_citations.py seed/brief.md seed/known_reporter.json"
```

`CommandGate` tokenizes the string and runs it without a shell (no
`|`/`&&` chaining — a gate needing shell features ships its own wrapper
script instead), and treats exit 0 as pass. The checker itself
(`seed/check_citations.py`) is pure standard library: it loads the trusted
reporter, derives valid citation patterns from it, scans the document for
`VOLUME REPORTER PAGE`-shaped citations, and fails (exit 1) if any citation
isn't in the reporter — catching both a mis-paginated real case and a
wholly fabricated one, which is exactly the shape behind the 1,600+
documented AI legal-hallucination sanctions the loop is modeling. This
pattern generalizes to any domain: a dependency-free Python (or shell)
script that mechanically checks a fact, without an LLM in the loop.

**pytest** — for loops whose done-condition is "the test suite passes."
Set `gate.kind: pytest`; `PytestGate` runs `pytest -q` in the workspace,
treating exit 1 (some tests failed) as a normal `Verdict(passed=False)` and
exit codes 2-5 (interrupted, internal error, usage error, no tests
collected) as a `GateError` — the gate couldn't run at all, a different
failure mode than "the agent hasn't fixed it yet."

## How the stub cassette replays a recorded fix

The keyless demo path uses `runner.default: stub`, which reads
`cassettes/default.json` — a JSON file with a `version: 1` envelope and an
`interactions` array. Each entry names a `lap` (1-indexed, or `"*"` as a
catch-all for any lap beyond the recorded ones), an `agent_output` string
(written to `agent_output.txt` in the workspace for gate/debug
inspection), an `actions` array (currently `write_file` or `noop` — the
only two action types `StubRunner` recognizes), an `agent_claimed_done`
flag, a `changed` flag, and a `tokens` count. `citation-existence-check`'s
cassette has exactly one real interaction: lap 1 rewrites `seed/brief.md`
to correct Miranda's page and remove the fabricated Thompson v. Halden
citation, with a `"*"` catch-all in case the gate somehow needs a second
lap. `StubRunner` genuinely applies these actions to the scratch
workspace (an earlier draft only wrote a log and never touched the
workspace, which meant the gate could never see a real fix) — it makes no
network calls, never modifies the cassette file, and refuses (raising
`RunnerError`) any action path that would escape the workspace or write to
a `forbid:`-protected file.

## The `forbid:` guard

`citation-existence-check`'s `loop.yaml` declares:

```yaml
forbid:
  - "seed/check_citations.py"
  - "seed/known_reporter.json"
```

These are glob patterns naming the gate's own verification anchor and its
ground-truth data — the whole point of the loop is that the agent conforms
the *document* to reality, never the checker or the reporter. This is
enforced twice, in depth: `StubRunner` itself refuses any cassette action
writing a forbidden path (case-insensitively, matching both the full
relative path and the basename), and — for every runner, not just the
stub — `AnchorGuardRunner` wraps whichever runner was chosen and, after
every single turn, re-scans the workspace to catch three tamper patterns:
a protected anchor file deleted or modified, a *new* file appearing that
matches a `forbid` glob, and (for pytest-style gates specifically) a
test-collection config file (`pyproject.toml`, `pytest.ini`, `tox.ini`,
`setup.cfg`, any `conftest.py`) planted or changed mid-run — because
redirecting what pytest collects can make a gate report a pass without
ever touching the anchor file's bytes. Any of these raises `RunnerError`,
which propagates as a non-DONE, non-zero-exit failure — the loop never
even reaches the gate against a tampered workspace.

## The L1 / L2 / L3 rung ladder

`rung` in `loop.yaml` sets the safety posture, and (unless
`bounds.require_approval` overrides it explicitly) derives whether a
passed gate still needs a human approval before the loop reports DONE:

- **L1 ("report")** — a human reads every verdict; no approval gate,
  because there's no autonomous action being approved in the first place.
- **L2 ("assisted")** — the agent acts, but the loop pauses at a passed
  gate for human approval before exiting DONE. `citation-existence-check`
  is L2 in its `loop.yaml`, but its `bounds.yaml` sets
  `require_approval: false` explicitly — the code comment is candid about
  why: "a passing citation check should gate human sign-off before a brief
  is filed, not auto-file... the keyless demo runs unattended so it can
  reach DONE offline; flip this to true when you wire a real runner." This
  is the correct pattern for a keyless showcase: don't misrepresent the
  rung, but don't block the demo on a prompt either.
- **L3 ("unattended")** — the most autonomous rung; approval is derived
  purely from `bounds`, with no rung-based default requiring it.

The same rung also derives sandbox and approval defaults for the
credentialed runners (`codex`'s `--sandbox` mode, `antigravity`'s approval
policy) when a loop hasn't set them explicitly — see
`composition._resolve_codex_sandbox_mode` and
`_resolve_antigravity_approve_policy`.

## Verify protocol

Before a new loop is considered done, run both of these — they are the
actual gate on the loop, not a suggestion:

```bash
bl lint loops/<your-loop>
```

`bl lint` validates `loop.yaml` + `bounds.yaml`: required keys present, a
keyless `runner.default` (`stub`/`shell`/`python_callable` — a Qualixar
product gate kind is rejected outright as a manifest default), and every
path (`spec`/`bounds`/`memory`/`cassette`) resolving inside the loop
folder. Exit 0 means it passed.

```bash
bl run loops/<your-loop> --yes
```

`--yes` skips the interactive trust-confirmation prompt (required for CI
or any non-interactive context; without it, `bl run` refuses to proceed).
A genuinely working loop prints:

```
✓ [DONE] gate-passed (laps: 1)  ledger: loops/<your-loop>/.ledger.jsonl
```

Verified live against `citation-existence-check` in this repo: `bl lint`
returns `[PASS]`, and `bl run --yes` reaches `✓ [DONE] gate-passed (laps: 1)`
on the very first lap, exactly as the cassette and gate predict. If your
loop doesn't reach this line, the gate is telling you something real is
still broken — don't hand-edit the ledger or the gate to force it.

## See also

- [ARCHITECTURE.md](./ARCHITECTURE.md) — how the runner/gate/bounds you
  configure here get wired into the engine.
- [NINE-BOUNDS.md](./NINE-BOUNDS.md) — the full field-by-field bounds
  reference.
