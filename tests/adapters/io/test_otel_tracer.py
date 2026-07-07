import pytest

# These tests require opentelemetry-sdk installed.
# If not installed they are SKIPPED (not FAILED) — the repo must stay keyless/depless.
otel = pytest.importorskip("opentelemetry.sdk", reason="opentelemetry-sdk not installed")

from pathlib import Path
from bounded_loops.adapters.io.otel_tracer import OtelTracer
from bounded_loops.domain.models import LoopContext, RunResult, Verdict, Rung

# In-memory span collector for assertions
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry import trace as otel_trace

def _make_tracer_with_exporter() -> tuple[OtelTracer, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    # We need to inject this provider into OtelTracer; use the internal hook.
    ot = OtelTracer.__new__(OtelTracer)
    ot._tracer = provider.get_tracer(__name__)
    ot._trace_module = otel_trace
    return ot, exporter

def _ctx(lap: int = 1, loop_name: str = "bug-fix-red-green") -> LoopContext:
    return LoopContext(
        workspace=Path("/tmp"), lap=lap, rung=Rung.L1,
        trace_id="t-xyz", env={"loop_name": loop_name}
    )
def _result(tokens: int = 250) -> RunResult:
    return RunResult(changed=True, agent_claimed_done=False, tokens=tokens)
def _verdict(passed: bool) -> Verdict:
    return Verdict(passed=passed, detail="ok", evidence={})

def test_span_name_format():
    ot, exporter = _make_tracer_with_exporter()
    ot.span(_ctx(loop_name="my-loop"), _result(), _verdict(True))
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "invoke_agent my-loop"

def test_span_attributes_on_pass():
    ot, exporter = _make_tracer_with_exporter()
    ot.span(_ctx(lap=3), _result(tokens=512), _verdict(True))
    attrs = exporter.get_finished_spans()[0].attributes
    assert attrs["gen_ai.operation.name"] == "invoke_agent"
    assert attrs["gen_ai.usage.input_tokens"] == 512
    assert attrs["gen_ai.response.finish_reasons"] == ("gate_pass",)
    assert attrs["bounded_loops.lap"] == 3
    assert attrs["bounded_loops.verdict"] == "pass"
    assert attrs["bounded_loops.trace.include_sensitive_data"] is False

def test_span_attributes_on_fail():
    ot, exporter = _make_tracer_with_exporter()
    ot.span(_ctx(), _result(), _verdict(False))
    attrs = exporter.get_finished_spans()[0].attributes
    assert attrs["gen_ai.response.finish_reasons"] == ("gate_fail",)
    assert attrs["bounded_loops.verdict"] == "fail"

def test_module_importable_without_otel(monkeypatch):
    """
    Importing the module must NOT fail even if opentelemetry is absent.
    We can't uninstall otel in a running test, so we verify that the lazy guard
    is in __init__ and NOT at module level by checking the source.
    """
    import inspect
    import bounded_loops.adapters.io.otel_tracer as mod
    src = inspect.getsource(mod)
    # The word 'opentelemetry' must NOT appear outside of a function/method body
    # at module top-level. A crude but effective check: the first import line
    # for opentelemetry must be indented (i.e., inside a function).
    lines = src.splitlines()
    for line in lines:
        if "from opentelemetry" in line or "import opentelemetry" in line:
            # Must be indented (inside a def/class body)
            assert line.startswith(" ") or line.startswith("\t"), (
                f"OTel import at module top level: {line!r}"
            )

def test_import_error_on_missing_otel(monkeypatch):
    """Instantiation raises ImportError when opentelemetry is absent."""
    import builtins
    real_import = builtins.__import__
    def blocked_import(name, *args, **kwargs):
        if name.startswith("opentelemetry"):
            raise ImportError(f"Blocked: {name}")
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", blocked_import)
    with pytest.raises(ImportError, match="opentelemetry-sdk is required"):
        OtelTracer()
