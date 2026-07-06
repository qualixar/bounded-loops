from bounded_loops.adapters.io.noop_tracer import NoopTracer
from bounded_loops.domain.models import LoopContext, RunResult, Verdict, Rung
from pathlib import Path

def _ctx() -> LoopContext:
    return LoopContext(
        workspace=Path("/tmp"), lap=1, rung=Rung.L1,
        trace_id="t-001", env={}
    )
def _result() -> RunResult:
    return RunResult(changed=True, agent_claimed_done=False, tokens=100, log="ok")
def _verdict() -> Verdict:
    return Verdict(passed=True, detail="ok", evidence={})

def test_noop_tracer_does_not_raise():
    tracer = NoopTracer()
    tracer.span(_ctx(), _result(), _verdict())  # must not raise

def test_noop_tracer_is_callable_with_zero_dependencies():
    # Confirm no opentelemetry import is triggered
    import sys
    before = set(sys.modules.keys())
    NoopTracer().span(_ctx(), _result(), _verdict())
    after = set(sys.modules.keys())
    otel_modules = {m for m in (after - before) if "opentelemetry" in m}
    assert not otel_modules, f"NoopTracer must not import otel: {otel_modules}"

def test_noop_tracer_returns_none():
    result = NoopTracer().span(_ctx(), _result(), _verdict())
    assert result is None

def test_noop_tracer_accepts_all_port_args():
    """Structural: ensure signature matches TracerPort exactly."""
    import inspect
    sig = inspect.signature(NoopTracer.span)
    params = list(sig.parameters.keys())
    # self, ctx, result, verdict — matching TracerPort.span
    assert params == ["self", "ctx", "result", "verdict"]
