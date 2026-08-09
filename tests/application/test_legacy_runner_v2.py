from __future__ import annotations

from pathlib import Path

from bounded_loops.application.legacy_runner_v2 import LegacyRunnerV2Adapter
from bounded_loops.application.ports import RunnerPortV2, RunningTurnPort
from bounded_loops.domain.models import (
    LoopContext,
    RunResult,
    Rung,
    Spec,
    TurnRequest,
    TurnState,
    UsageState,
)


class _LegacyRunner:
    def run_once(self, spec: Spec, ctx: LoopContext) -> RunResult:
        assert spec.name == "example"
        assert ctx.lap == 1
        return RunResult(changed=True, agent_claimed_done=False, tokens=12, log="legacy output")


def test_legacy_runner_is_an_explicit_completed_v2_turn() -> None:
    request = TurnRequest(
        spec=Spec(name="example", goal="goal", steps=("step",), stop_condition="gate"),
        context=LoopContext(workspace=Path("/tmp/ws"), lap=1, rung=Rung.L1, trace_id="t"),
    )
    adapter = LegacyRunnerV2Adapter(_LegacyRunner())

    assert isinstance(adapter, RunnerPortV2)
    running = adapter.start(request)
    assert isinstance(running, RunningTurnPort)
    assert running.poll() is TurnState.COMPLETED
    assert running.wait(timeout_s=0).stdout == "legacy output"
    assert running.wait().usage_state is UsageState.MEASURED
