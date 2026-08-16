#!/usr/bin/env python3
"""Adapt an arg-style agent CLI to the stdin-style contract ``ShellRunner`` expects.

WHY THIS EXISTS, and why it is in the experiment rather than in the engine.

``ShellRunner`` feeds the prompt on **stdin**. Of the five agent CLIs the project
ships profiles for, only ``claude`` reads stdin; ``agy``, ``grok`` and ``muse`` each
require the prompt as a positional argument and exit non-zero without one (probed
live 2026-08-16: "flag needs an argument: -p", "missing prompt", "a value is
required for '--single <PROMPT>'"). So the base loop engine can drive exactly one
of the five today, and E5 needs four.

The tempting fix is a new runner class per CLI. That would put a second copy of the
provider table in the base engine, and a duplicated table that drifts from its
original is the precise defect class this paper is about — we would be committing it
in the act of measuring it.

Instead this shim imports the SHIPPED profile table and the SHIPPED pure argv
builder, so there is exactly one definition of how each CLI is invoked, and the
ordering rule it encodes (usage_args go last, after the prompt, or grok's ``-p``
swallows the flag) is enforced by the same tested function the graph connector uses.

It is invoked through the engine's own security allowlist rather than around it:
``AGENT_CMD_ALLOWLIST`` vouches for ``python3`` as a first token, so a loop manifest
may legally say

    runner:
      default: shell
      agent_cmd: "python3 /abs/path/e5_cli_shim.py grok"

This stays in the experiment directory. Shipping it would be shipping a workaround;
the product fix is a generic local-CLI runner over the shared profile table, tracked
separately.

Usage:  <prompt on stdin> | python3 e5_cli_shim.py <profile-name>
Exit code and stdout are the CLI's own, unmodified.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _load_shipped_profiles():
    """Import the profile table and argv builder from the installed package.

    Deliberately not vendored. If a profile changes upstream, this experiment
    changes with it or fails loudly — it cannot silently measure a stale one.
    """
    from bounded_loops.graph.adapters.connectors.local_cli_worker import (  # noqa: PLC0415
        CLI_PROFILES,
        build_cli_argv,
    )

    return CLI_PROFILES, build_cli_argv


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {Path(argv[0]).name} <profile-name>", file=sys.stderr)
        return 2

    profile_name = argv[1]
    profiles, build = _load_shipped_profiles()
    if profile_name not in profiles:
        print(
            f"unknown profile {profile_name!r}; shipped: {sorted(profiles)}",
            file=sys.stderr,
        )
        return 2

    prompt = sys.stdin.read()
    if not prompt.strip():
        # A silently-empty prompt is how the USER-allowlist defect produced a loop
        # that spent its whole budget having never asked the agent anything. Fail
        # loudly rather than reproduce that shape inside the experiment measuring it.
        print("refusing to invoke a CLI with an empty prompt", file=sys.stderr)
        return 2

    profile = profiles[profile_name]
    cli_argv, stdin_text = build(profile, prompt, binary=profile.binary)

    # DOCUMENTED DIVERGENCE, and the only one in this shim.
    #
    # The shipped graph profile for `agy` carries neither a permission flag nor a
    # workspace flag, and without them agy is a no-op that reports success:
    #   * with no auto-approval it answers "no output produced — a tool required
    #     the \"command\" permission that headless mode cannot prompt for, so it
    #     was auto-denied", exits 0, and changes nothing;
    #   * with no --add-dir it ignores the process cwd entirely and writes into
    #     ~/.gemini/antigravity-cli/scratch/, reporting "Created and verified"
    #     with a file:// URL pointing outside the workspace the gate reads.
    #
    # Both were probed live on 2026-08-16 and both are fixed in the base
    # AntigravityRunner. The graph profile still lacks them and is filed as a
    # defect. Compensating here rather than silently reporting agy as a provider
    # that fails to converge: publishing that would have been a false claim about
    # a third party's product caused by our own misconfiguration — precisely the
    # error this experiment exists to avoid making about gates.
    if profile_name == "agy":
        cli_argv += ["--dangerously-skip-permissions", "--add-dir", str(Path.cwd())]

    try:
        completed = subprocess.run(
            cli_argv,
            input=stdin_text if stdin_text is not None else "",
            capture_output=True,
            text=True,
            timeout=900,
        )
    except FileNotFoundError:
        print(f"{profile.binary!r} is not on PATH", file=sys.stderr)
        return 2
    except subprocess.TimeoutExpired:
        print(f"{profile.binary!r} exceeded the 900s shim timeout", file=sys.stderr)
        return 2

    sys.stdout.write(completed.stdout)
    sys.stderr.write(completed.stderr)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
