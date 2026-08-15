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
  2 = execution interrupted         -> SPLIT, see below
  3 = internal pytest error         -> GateError
  4 = command-line usage error      -> GateError
  5 = no tests collected            -> GateError

Exit 2 means two different things and pytest does not distinguish them in
the code:

  a) COLLECTION FAILED — an import raised, so the suite could not even be
     assembled. This is the worker's fault and is the single most common
     way a bounded loop's edit goes wrong: empty the module under test and
     `from mod import thing` raises ImportError.
  b) INTERRUPTED — a human pressed Ctrl-C, or a plugin aborted the session.

They must be classified differently, because the two answers to "whose
fault is this" lead to opposite actions. (a) is a verdict the worker can
act on and must be handed back so the loop retries. (b) is the run being
stopped from outside, and retrying it would loop against a session nobody
is watching.

The industry rule this follows: a failure caused by the work is returned
to the worker rather than raised past it, because a swallowed error
becomes a silent failure or a hallucinated success. Anthropic states it
for tool results; AWS Step Functions and Azure Durable Functions state it
as transient-vs-permanent; Terraform states it as three exit codes rather
than two. See `docs/gate-verdict-contract.md`.

Distinguished by pytest's own report line — `Interrupted: N error(s)
during collection` — with the SAFE direction as the default: an exit 2
this cannot explain stays a GateError. A misread here would either loop
forever against a broken environment (bad) or halt on a fixable edit
(today's behaviour), and defaulting to the latter keeps the change
strictly smaller than the defect it removes.
"""

from __future__ import annotations

import shlex
import sys

from bounded_loops.adapters.gates.command import CommandGate
from bounded_loops.domain.errors import GateError
from bounded_loops.domain.models import LoopContext, Verdict

#: pytest's wording when a collection error — not an interruption — caused exit 2. Present in the
#: `Interrupted: 1 error during collection` banner and in the short summary's `ERROR <file>` block.
_COLLECTION_ERROR_MARKER = "during collection"

#: pytest's INTERRUPTED code, which conflates a failed import with a Ctrl-C.
_INTERRUPTED = 2


class PytestGate(CommandGate):
    """Runs `pytest -q [extra_args]`; exit 1 is a normal gate fail, exit 2 depends on why."""

    #: 2 is admitted here so the base class returns a Verdict instead of raising, and `check` below
    #: re-raises for the interruption case. Admitting it wholesale would report a Ctrl-C as the
    #: work having failed.
    EXPECTED_FAIL_CODES: frozenset[int] = frozenset({1, _INTERRUPTED})

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
        """Run pytest through this package's interpreter and record that fact.

        Re-raises the half of exit 2 that is genuinely an interruption, so admitting 2 into
        `EXPECTED_FAIL_CODES` widens what counts as a gate FAIL by exactly one case — a suite that
        could not be collected — and nothing else.
        """
        verdict = super().check(ctx)

        if verdict.evidence.get("code") == _INTERRUPTED:
            tail = str(verdict.evidence.get("tail", ""))
            if _COLLECTION_ERROR_MARKER not in tail:
                raise GateError(
                    f"PytestGate: pytest exited {_INTERRUPTED} (interrupted) without reporting a "
                    "collection error, so the session was stopped from outside rather than by a "
                    "fault in the work. Treated as a gate error, not a failed check, because "
                    f"retrying cannot fix it. tail={tail[-500:]!r}"
                )
            return Verdict(
                passed=False,
                detail=(
                    "gate failed (exit 2): the test suite could not be collected — an import "
                    "raised. Fix the module under test; pytest via current Python module"
                ),
                evidence={
                    **verdict.evidence,
                    "invocation": "current-python-module",
                    "failure_kind": "collection-error",
                },
            )

        return Verdict(
            passed=verdict.passed,
            detail=f"{verdict.detail}; pytest via current Python module",
            evidence={**verdict.evidence, "invocation": "current-python-module"},
        )
