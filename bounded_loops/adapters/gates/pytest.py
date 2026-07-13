"""
PytestGate — thin, zero-logic subclass of CommandGate hardcoded to
"pytest -q".

Self-contained by design: PytestGate.
EXPECTED_FAIL_CODES is its OWN class attribute, not imported from
command.py — zero cross-file constant coupling, no risk of the two
gates' fail-code sets silently drifting apart via a shared mutable
import. This module imports nothing from command.py except the
CommandGate class itself.

pytest exit codes (stable since pytest 3.x):
  0 = all tests passed              -> Verdict(passed=True)
  1 = some tests failed             -> Verdict(passed=False)  (a NORMAL
      gate fail — the teaching contrast: the agent fixed some but not
      all tests)
  2 = execution interrupted         -> GateError
  3 = internal pytest error         -> GateError
  4 = command-line usage error      -> GateError
  5 = no tests collected            -> GateError
"""

from __future__ import annotations

import shlex
import sys

from bounded_loops.adapters.gates.command import CommandGate
from bounded_loops.domain.models import LoopContext, Verdict


class PytestGate(CommandGate):
    """Runs `pytest -q [extra_args]`; exit 1 is a normal gate fail."""

    EXPECTED_FAIL_CODES: frozenset[int] = frozenset({1})

    def __init__(self, extra_args: str = "", timeout_s: int = 120) -> None:
        cmd_parts = [sys.executable, "-m", "pytest", "-q"]
        if extra_args.strip():
            cmd_parts.extend(shlex.split(extra_args))
        cmd = shlex.join(cmd_parts)

        super().__init__(
            cmd=cmd,
            expected_fail_codes=PytestGate.EXPECTED_FAIL_CODES,
            timeout_s=timeout_s,
        )

    def check(self, ctx: LoopContext) -> Verdict:
        """Run pytest through this package's interpreter and record that fact."""
        verdict = super().check(ctx)
        return Verdict(
            passed=verdict.passed,
            detail=f"{verdict.detail}; pytest via current Python module",
            evidence={**verdict.evidence, "invocation": "current-python-module"},
        )
