"""Token counts come out of a provider response; nothing else does.

The forwarder is the only component that ever holds response bytes, so this is the one place
usage can be read. It parses UNTRUSTED input, and anything it cannot read confidently becomes
None — which makes a budgeted node fail closed rather than run on numbers of unknown
provenance.
"""

from __future__ import annotations

import json

import pytest

from bounded_loops.graph.adapters.connectors.provider_usage import extract_provider_usage


def _body(document: object) -> bytes:
    return json.dumps(document).encode("utf-8")


def test_an_openai_shaped_response_is_read() -> None:
    usage = extract_provider_usage(
        _body({"usage": {"prompt_tokens": 120, "completion_tokens": 40, "total_tokens": 160}}),
        reported_by="provider:api.openai.com",
    )

    assert usage is not None
    assert (usage.input_tokens, usage.output_tokens, usage.total_tokens) == (120, 40, 160)
    assert usage.reported_by == "provider:api.openai.com"


def test_an_anthropic_shaped_response_is_read() -> None:
    """Same container name as OpenAI, different field names — so the container match alone
    cannot decide the dialect."""
    usage = extract_provider_usage(
        _body({"usage": {"input_tokens": 900, "output_tokens": 33}}),
        reported_by="provider:api.anthropic.com",
    )

    assert usage is not None
    assert usage.total_tokens == 933


def test_a_gemini_shaped_response_is_read() -> None:
    usage = extract_provider_usage(
        _body({"usageMetadata": {
            "promptTokenCount": 12, "candidatesTokenCount": 8, "totalTokenCount": 20,
        }}),
        reported_by="provider:generativelanguage.googleapis.com",
    )

    assert usage is not None
    assert usage.total_tokens == 20


def test_nothing_but_numbers_crosses_back() -> None:
    """The no-response-bytes rule: a count is metering metadata, model output is content."""
    usage = extract_provider_usage(
        _body({
            "choices": [{"message": {"content": "the model's actual answer, which must not leak"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 7},
        }),
        reported_by="provider:api.openai.com",
    )

    assert usage is not None
    assert "answer" not in json.dumps(usage.payload())
    assert set(usage.payload()) <= {
        "input_tokens", "output_tokens", "cost_microunits", "wallclock_ms", "reported_by",
        "estimated_cost_microunits", "estimated_by",
    }


@pytest.mark.parametrize(
    "body",
    [
        b"not json at all",
        b"",
        b"\xff\xfe invalid utf-8",
        b'"a bare string"',
        b"[1, 2, 3]",
        b'{"usage": "not an object"}',
        b'{"choices": []}',                                   # no usage block
        b'{"usage": {"some_other_dialect": 5}}',              # unknown shape
        b'{"usage": {"prompt_tokens": -5, "completion_tokens": 1}}',    # hostile negative
        b'{"usage": {"prompt_tokens": 1.5, "completion_tokens": 1}}',   # not an integer
        b'{"usage": {"prompt_tokens": true, "completion_tokens": 1}}',  # bool is an int subclass
        b'{"usage": {"prompt_tokens": "1000", "completion_tokens": "1"}}',  # numeric string
    ],
)
def test_anything_unreadable_is_unmeasured_rather_than_guessed(body: bytes) -> None:
    """A budgeted node then fails closed with a clear message. A run never crashes here."""
    assert extract_provider_usage(body, reported_by="provider:host") is None


def test_a_hostile_count_cannot_enter_a_spend_total() -> None:
    """A negative charge would REFUND budget and buy attempts past the cap."""
    assert extract_provider_usage(
        _body({"usage": {"prompt_tokens": 10, "completion_tokens": -1_000_000}}),
        reported_by="provider:host",
    ) is None


def test_elapsed_time_is_reported_even_when_tokens_cannot_be(tmp_path) -> None:
    """Honest and useful — and it does NOT satisfy a token cap, since caps are per dimension."""
    usage = extract_provider_usage(
        b'{"choices": []}', reported_by="provider:host", wallclock_ms=1_234,
    )

    assert usage is not None
    assert usage.wallclock_ms == 1_234
    assert usage.total_tokens is None


def test_a_partial_count_is_kept_as_partial() -> None:
    """One side measured, the other not: the total stays unknown rather than becoming the half."""
    usage = extract_provider_usage(
        _body({"usage": {"prompt_tokens": 100}}), reported_by="provider:host",
    )

    assert usage is not None
    assert usage.input_tokens == 100
    assert usage.output_tokens is None
    assert usage.total_tokens is None
