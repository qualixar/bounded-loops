"""
EnvKillSwitch — concrete `KillSwitchPort` adapter.

Resolves : an env var, not a workspace-local file, is
the kill signal. Rationale: a `.kill` sentinel
inside the loop's workspace sits in the same trust boundary as
agent-writable content — an untrusted/misbehaving agent could touch or
delete it. An environment variable is outside the agent's filesystem
reach entirely.
"""

from __future__ import annotations

import os


class EnvKillSwitch:
    """
    Implements KillSwitchPort. Polled once per lap.

    Trips when the environment variable BOUNDED_LOOPS_KILL is set to any
    non-empty value. The env var is read fresh each call via
    os.environ.get — a parent supervisor process can mutate the env of a
    child it controls, or a test can monkeypatch os.environ directly.
    """

    ENV_VAR = "BOUNDED_LOOPS_KILL"

    def tripped(self) -> bool:
        return bool(os.environ.get(self.ENV_VAR, ""))
