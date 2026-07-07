# TOP-LEVEL imports: ONLY stdlib and bounded_loops domain — NO opentelemetry here.
from __future__ import annotations
from bounded_loops.domain.models import LoopContext, RunResult, Verdict

# OTel attribute key constants (single source of truth — update here if semconv renames)
_OP_NAME       = "gen_ai.operation.name"
_AGENT_NAME    = "gen_ai.agent.name"
_INPUT_TOKENS  = "gen_ai.usage.input_tokens"
_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
_FINISH        = "gen_ai.response.finish_reasons"
_BL_LAP        = "bounded_loops.lap"
_BL_TRACE_ID   = "bounded_loops.trace_id"
_BL_VERDICT    = "bounded_loops.verdict"
_BL_SENSITIVE  = "bounded_loops.trace.include_sensitive_data"


class OtelTracer:
    """
    Implements TracerPort using OpenTelemetry GenAI semantic conventions.

    Lazy import: opentelemetry-sdk is imported in __init__, NOT at module level.
    This allows 'from bounded_loops.adapters.io.otel_tracer import OtelTracer'
    to succeed even when opentelemetry is not installed.
    The import fails (ImportError) only when OtelTracer() is instantiated.

    Usage:
        tracer = OtelTracer(service_name="bounded-loops")
        # Then inject into composition.wire(... tracer=tracer)
    """

    def __init__(self, service_name: str = "bounded-loops") -> None:
        # --- lazy import block START ---
        try:
            from opentelemetry import trace  # type: ignore[import-not-found]
            from opentelemetry.sdk.trace import TracerProvider  # type: ignore[import-not-found]
            from opentelemetry.sdk.trace.export import BatchSpanProcessor  # type: ignore[import-not-found]  # noqa: F401
            from opentelemetry.sdk.resources import Resource  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ImportError(
                "opentelemetry-sdk is required for OtelTracer. "
                "Install it: pip install 'bounded-loops[otel]'"
            ) from exc
        # --- lazy import block END ---

        resource = Resource(attributes={"service.name": service_name})
        provider = TracerProvider(resource=resource)
        # No exporter configured by default — caller MUST add one via
        #   provider.add_span_processor(BatchSpanProcessor(your_exporter))
        # before passing to composition.  Default = spans go nowhere (in-memory only),
        # which is safe for keyless runs.
        self._tracer = trace.get_tracer(__name__, tracer_provider=provider)
        self._trace_module = trace  # keep reference for SpanKind

    def span(
        self,
        ctx: LoopContext,
        result: RunResult,
        verdict: Verdict,
    ) -> None:
        loop_name = ctx.env.get("loop_name", "unknown")
        span_name = f"invoke_agent {loop_name}"
        SpanKind = self._trace_module.SpanKind

        with self._tracer.start_as_current_span(
            span_name, kind=SpanKind.INTERNAL
        ) as sp:
            sp.set_attribute(_OP_NAME,      "invoke_agent")
            sp.set_attribute(_AGENT_NAME,   f"{ctx.rung.value}:{loop_name}")
            sp.set_attribute(_INPUT_TOKENS,  result.tokens)
            sp.set_attribute(_OUTPUT_TOKENS, 0)
            sp.set_attribute(_FINISH,        ["gate_pass" if verdict.passed else "gate_fail"])
            sp.set_attribute(_BL_LAP,        ctx.lap)
            sp.set_attribute(_BL_TRACE_ID,   ctx.trace_id)
            sp.set_attribute(_BL_VERDICT,    "pass" if verdict.passed else "fail")
            sp.set_attribute(_BL_SENSITIVE,  False)
        # Span ends (and is exported) on context manager exit.
