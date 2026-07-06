"""
Error taxonomy for bounded-loops.

IMPORTANT SEMANTIC NOTE:
  A gate FAIL is a normal Verdict(passed=False), NOT an exception.
  GateError is raised ONLY when the gate itself cannot execute (e.g.
  binary missing, timeout, subprocess crash).
"""


class BoundedLoopsError(Exception):
    """Base exception for all bounded-loops errors."""
    # No extra fields — kept minimal so subclasses are free to specialise.


class ManifestError(BoundedLoopsError):
    """
    Raised when loop.yaml or bounds.yaml is missing, structurally invalid,
    or violates a validation rule (e.g. runner.default is 'claude-code'
    rather than 'stub'|'shell').

    Typical message pattern:
        ManifestError("loops/bug-fix-red-green/loop.yaml: runner.default "
                      "must be 'stub' or 'shell', got 'claude-code'")
    """


class RunnerError(BoundedLoopsError):
    """
    Raised when a runner adapter fails to execute (e.g. subprocess crash,
    timeout before the agent produced any output, missing binary).
    Normal completion with any exit code is NOT a RunnerError.
    """


class GateError(BoundedLoopsError):
    """
    Raised when the gate itself cannot execute (e.g. pytest binary missing,
    shell command not found, timeout before gate produced any output).

    A gate that RUNS and returns exit != 0 produces Verdict(passed=False) —
    that is NOT a GateError.

    Typical message pattern:
        GateError("pytest -q could not run (code 127): bash: pytest: "
                  "command not found")
    """


class KillSwitchTripped(BoundedLoopsError):
    """
    Raised (or caught internally, depending on caller policy) when the
    external kill switch is tripped between laps.

    The RunLoopUseCase catches this and returns Outcome(status=KILLED, ...).
    Adapters that detect the trip via a signal or file sentinel raise this
    to propagate it upward cleanly.
    """
