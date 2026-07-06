"""
AutoApproval + CliApproval — concrete `ApprovalPort` adapters.
"""

from __future__ import annotations

from bounded_loops.domain.models import LoopContext, Verdict


class AutoApproval:
    """
    Implements ApprovalPort. Always grants — used at L1 (report rung,
    approval not required) or when bounds.require_approval=False.
    """

    def granted(self, verdict: Verdict, ctx: LoopContext) -> bool:
        return True


class CliApproval:
    """
    Implements ApprovalPort. Used at L2/L3 when approval IS required.
    Prints the gate verdict to stdout and blocks on a stdin y/N prompt.
    In a non-interactive context (CI, piped stdin) input() raises
    EOFError — treated as "not granted" (fail closed, never fail open).
    """

    def granted(self, verdict: Verdict, ctx: LoopContext) -> bool:
        print(f"\n[bounded-loops] Gate passed: {verdict.detail}")
        print("[bounded-loops] Approve this result and mark DONE? [y/N] ", end="", flush=True)
        try:
            answer = input().strip().lower()
        except EOFError:
            return False  # non-interactive stdin — fail closed, not open
        return answer in ("y", "yes")
