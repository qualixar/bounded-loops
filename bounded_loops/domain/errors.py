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


class WallclockExceeded(BoundedLoopsError):
    """
    Raised by a runner when the loop's DECLARED `bounds.max_wallclock_s` expires
    part-way through an attempt.

    Deliberately NOT a subclass of RunnerError. A runner error means the harness
    failed to do its job; this means the harness did its job — the operator asked
    for a spend ceiling and the ceiling was reached. The controller turns it into
    Status.HALT with the bound named, never Status.ERROR, so a run that stopped
    because it was told to cannot be read as a run that broke.

    Typical message pattern:
        WallclockExceeded("wallclock limit 120s exceeded during an attempt "
                          "(ShellRunner, cmd='python3 seed/worker.py')")
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


class EvidenceError(BoundedLoopsError):
    """Raised when controller evidence is corrupt, altered, or inconsistent."""


class KillSwitchTripped(BoundedLoopsError):
    """Reserved for an adapter that prefers to RAISE when an external kill signal is seen.

    **Nothing in this engine raises it, and nothing catches it.** The kill switch is implemented by
    POLLING: `KillSwitchPort.tripped()` returns a bool and `RunLoopUseCase` checks it at the top of
    every lap before any work, recording a `decision: "killed"` row with `attempted: false`. That is
    the whole mechanism.

    This docstring previously claimed "The RunLoopUseCase catches this and returns
    Outcome(status=KILLED, ...)" and that adapters raise it. Neither was true — the class is
    referenced nowhere but here, its own test, and two architecture documents. Documentation
    asserting behaviour that does not exist is the declared-versus-enforced defect this project
    publishes about, committed in the one place a reader takes on trust.

    Kept rather than deleted because it is imported by `tests/domain/test_errors.py` and named in
    `docs/ARCHITECTURE.md` and the ports-and-adapters diagram, so removal would break an embedder
    that imports it. An adapter that raises this instead of returning True from `tripped()` will NOT
    be handled by the engine — the exception propagates. Use the port.
    """
