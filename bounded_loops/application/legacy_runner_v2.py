"""Compatibility bridge from synchronous RunnerPort to the F0.2 V2 contract."""

from __future__ import annotations

from bounded_loops.application.ports import RunnerPort, RunningTurnPort
from bounded_loops.domain.models import TurnRequest, TurnResult, TurnState, UsageState


class _CompletedLegacyTurn:
    def __init__(self, result: TurnResult) -> None:
        self._result = result

    def poll(self) -> TurnState:
        return self._result.state

    def cancel(self, _reason: str) -> None:
        """A legacy turn has completed synchronously before this method exists."""

    def wait(self, timeout_s: float | None = None) -> TurnResult:
        del timeout_s
        return self._result


class LegacyRunnerV2Adapter:
    """Expose existing synchronous runners through RunnerPortV2 during migration.

    This adapter is intentionally a compatibility boundary, not a claim of
    asynchronous cancellation. New subprocess-backed runners use ProcessTurn;
    callers can detect this bridge from its immediately terminal result.
    """

    def __init__(self, runner: RunnerPort) -> None:
        self._runner = runner

    def start(self, request: TurnRequest) -> RunningTurnPort:
        result = self._runner.run_once(request.spec, request.context)
        return _CompletedLegacyTurn(
            TurnResult(
                state=TurnState.COMPLETED,
                returncode=None,
                stdout=result.log,
                stderr="",
                output_truncated=False,
                usage_state=(UsageState.MEASURED if result.tokens > 0 else UsageState.UNKNOWN),
            )
        )
