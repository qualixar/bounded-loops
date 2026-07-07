from __future__ import annotations

import pytest

from bounded_loops.adapters.gates.composite import CompositeGate
from bounded_loops.domain.errors import GateError
from bounded_loops.domain.models import LoopContext, Rung, Verdict


class StaticGate:
    def __init__(self, verdict: Verdict) -> None:
        self._verdict = verdict

    def check(self, ctx: LoopContext) -> Verdict:
        return self._verdict


class BrokenGate:
    def check(self, ctx: LoopContext) -> Verdict:
        raise GateError("child could not run")


def _ctx(tmp_path) -> LoopContext:
    return LoopContext(workspace=tmp_path, lap=1, rung=Rung.L1, trace_id="t")


def test_composite_all_passes_when_all_children_pass(tmp_path):
    gate = CompositeGate([
        StaticGate(Verdict(True, "one")),
        StaticGate(Verdict(True, "two")),
    ])

    verdict = gate.check(_ctx(tmp_path))

    assert verdict.passed is True
    assert len(verdict.evidence["children"]) == 2


def test_composite_all_fails_when_any_child_fails(tmp_path):
    gate = CompositeGate([
        StaticGate(Verdict(True, "one")),
        StaticGate(Verdict(False, "two failed")),
    ])

    verdict = gate.check(_ctx(tmp_path))

    assert verdict.passed is False
    assert "1 child" in verdict.detail


def test_composite_requires_children():
    with pytest.raises(GateError, match="at least one"):
        CompositeGate([])


def test_composite_rejects_unknown_mode():
    with pytest.raises(GateError, match="mode='all'"):
        CompositeGate([StaticGate(Verdict(True, "ok"))], mode="any")


def test_composite_propagates_child_gate_error(tmp_path):
    gate = CompositeGate([BrokenGate()])

    with pytest.raises(GateError, match="child could not run"):
        gate.check(_ctx(tmp_path))