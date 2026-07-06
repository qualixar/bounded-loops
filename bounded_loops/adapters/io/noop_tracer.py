from bounded_loops.domain.models import LoopContext, RunResult, Verdict


class NoopTracer:
    """
    Implements TracerPort.
    Default tracer — zero dependencies, zero side effects.
    Activates whenever bounds.yaml trace:true AND no OTel SDK is configured.
    Composition root (composition.py) selects NoopTracer by default;
    OtelTracer is wired only when the caller passes trace_exporter explicitly.
    """

    def span(
        self,
        ctx: LoopContext,
        result: RunResult,
        verdict: Verdict,
    ) -> None:
        pass  # intentional no-op
