"""CompositeGate — combines multiple independent gates into one verdict."""

from __future__ import annotations

from bounded_loops.application.ports import GatePort
from bounded_loops.domain.errors import GateError
from bounded_loops.domain.models import LoopContext, Verdict


class CompositeGate:
    """Runs child gates and combines their verdicts.

    v1 supports `mode="all"`: every child gate must pass. A child gate
    raising GateError remains a GateError, because the composite gate itself
    could not complete its verification.
    """

    def __init__(self, gates: list[GatePort], mode: str = "all") -> None:
        if not gates:
            raise GateError("CompositeGate requires at least one child gate")
        if mode != "all":
            raise GateError("CompositeGate v1 supports only mode='all'")
        self._gates = tuple(gates)
        self._mode = mode

    def check(self, ctx: LoopContext) -> Verdict:
        child_verdicts = [gate.check(ctx) for gate in self._gates]
        evidence = {
            "mode": self._mode,
            "children": [
                {
                    "passed": verdict.passed,
                    "detail": verdict.detail,
                    "evidence": verdict.evidence,
                }
                for verdict in child_verdicts
            ],
        }
        failed = [verdict for verdict in child_verdicts if not verdict.passed]
        if failed:
            return Verdict(
                passed=False,
                detail=f"composite gate failed: {len(failed)} child gate(s) failed",
                evidence=evidence,
            )
        return Verdict(
            passed=True,
            detail=f"composite gate passed: {len(child_verdicts)} child gate(s) passed",
            evidence=evidence,
        )