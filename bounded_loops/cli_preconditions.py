"""Refusals that must happen before `bl run` asks the operator to confirm anything.

Its own module because `cli.py` sits on this project's 800-line cap and because these
checks are a growing set with one rule in common: they are things the operator cannot
act on from the prompt, so asking first wastes their decision.

Quarantine consent was originally checked inside `wire()`, which runs *after* the trust
prompt. A loop that could not start was therefore announced, confirmed, and only then
refused — the person had already answered "I trust this loop" before being told it would
not run. Found by a usability review running the command rather than reading it.
"""

from __future__ import annotations

import sys
from pathlib import Path

from bounded_loops.application.manifest import LoopManifest
from bounded_loops.application.quarantine_consent import require_quarantine_consent
from bounded_loops.domain.errors import ManifestError
from bounded_loops.trust_store import record_trust


def check_run_preconditions(loop_dir: Path, manifest: LoopManifest) -> str | None:
    """Return an operator-facing message, or None to proceed with the run."""
    try:
        require_quarantine_consent(loop_dir, manifest.bounds.quarantine_inputs)
    except ManifestError as exc:
        return f"bl run: {exc}"
    return None


def _confirm_trust(
    manifest: LoopManifest,
    skip_prompt: bool,
    *,
    runner_override: str | None = None,
) -> bool:
    """
    Security fix: a
    loop.yaml's gate.run (or runner.agent_cmd for shell) is arbitrary shell
    code, sourced from a folder bounded-loops explicitly invites as a
    community PR. Print exactly what will run before running it — a
    direnv-style trust gate — rather than silently executing an unfamiliar
    loop's command.

    Fails CLOSED: if stdin is not a TTY and --yes was not passed (the CI
    case), this returns False rather than guessing "probably fine."

    Trust recording: a genuine interactive 'y' answer is a
    real human review event, so it records a trust entry that the
    verify-on-stop hook will later recognize for this exact loop_dir + gate
    command. --yes (skip_prompt) is a CI bypass, NOT a human review event —
    it must never record trust on its own.
    """
    gate_cmd = manifest.gate_config.get("run", f"<{manifest.gate_kind} gate>")
    effective_runner = runner_override or manifest.runner_kind
    print(f"[bounded-loops] About to run loop '{manifest.name}':")
    print(f"  runner : {effective_runner}")
    print(f"  gate   : {gate_cmd}")
    if skip_prompt:
        return True   # --yes: CI bypass, NOT a human review — no trust recorded
    if not sys.stdin.isatty():
        return False   # non-interactive + no --yes → fail closed, never fail open
    answer = input("Proceed? [y/N] ").strip().lower()
    confirmed = answer in ("y", "yes")
    if confirmed:
        record_trust(manifest.loop_dir, gate_cmd)   # NEW — the only line added
    return confirmed
