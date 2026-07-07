# bounded-loops Project Instructions

This repository is a production-oriented AI loop harness. The core invariant is: an agent never decides its own completion. Only an independent gate can produce `DONE`.

Follow these rules for all work in this repo:

- Preserve the hexagonal architecture: domain stays pure, application depends only on domain and ports, adapters implement ports, and `composition.py` is the only composition root.
- Do not bypass gates or treat `agent_claimed_done` as success.
- Prefer `python3 -m bounded_loops.cli ...` in tests and docs unless explicitly testing installed console scripts.
- New runners and gates must classify outcomes as pass, normal fail, or execution error. Do not silently pass on missing tools, empty scanner output, malformed reports, or unknown exit codes.
- New loop examples must include a real gate, broken seed, production safety posture, and instructions for adapting the loop to a real repo.
- Do not put secrets or machine-specific absolute paths in docs, fixtures, cassettes, or screenshots.
- Keep contributed loops keyless by default unless the loop clearly declares required external tools, network, or credentials.

Before claiming a change is done, run the narrowest relevant executable check. For loop examples, run `python3 -m bounded_loops.cli lint <loop-dir>` and `python3 -m bounded_loops.cli run <loop-dir> --yes` where possible.