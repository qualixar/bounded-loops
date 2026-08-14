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


class TestProviderReportedCost:
    """A provider that tells us what it charged — found by running the real BYOK path.

    OpenRouter returns ``usage.cost`` in USD alongside its token counts. The extractor read only
    tokens, so a node declaring ``max_cost_microunits`` on that connection **paid for the call and
    then failed as budget_unmeasurable**, with the exact cost sitting unread in the response. Same
    shape P2-B found on the ``claude`` CLI, on the other transport — which is the argument for
    testing both paths against real providers rather than one.
    """

    def test_a_reported_cost_becomes_the_metered_cost(self) -> None:
        body = json.dumps({
            "choices": [{"message": {"content": "pong"}}],
            "usage": {"prompt_tokens": 17, "completion_tokens": 2, "cost": 0.25},
        }).encode("utf-8")

        usage = extract_provider_usage(body, reported_by="provider:openrouter.ai")

        assert usage is not None
        assert usage.cost_microunits == 250_000

    def test_a_sub_micro_charge_rounds_UP_not_to_free(self) -> None:
        """The measured real value: 4.158e-07 USD is 0.4158 micro-USD. Truncating makes a real
        charge free, and free is the direction that lets a cap permit unauthorised spend."""
        body = json.dumps({"usage": {"prompt_tokens": 17, "completion_tokens": 2, "cost": 4.158e-07}}).encode()

        usage = extract_provider_usage(body, reported_by="provider:openrouter.ai")

        assert usage is not None
        assert usage.cost_microunits == 1

    def test_a_two_decimal_amount_is_exact(self) -> None:
        """``Decimal(str(x))``, not binary float arithmetic: ``0.1 * 1_000_000`` is not 100000 for
        1196 of the two-decimal amounts under $10."""
        body = json.dumps({"usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 2.01}}).encode()

        usage = extract_provider_usage(body, reported_by="p")

        assert usage is not None
        assert usage.cost_microunits == 2_010_000

    @pytest.mark.parametrize("cost", [-1, True, "0.5", None, [0.5], {"a": 1}])
    def test_a_cost_that_is_not_a_number_is_unmeasured_not_guessed(self, cost: object) -> None:
        """Tokens still count; only the cost dimension goes unmeasured, so a token cap keeps
        working while a cost cap on the same node fails closed."""
        body = json.dumps({"usage": {"prompt_tokens": 5, "completion_tokens": 5, "cost": cost}}).encode()

        usage = extract_provider_usage(body, reported_by="p")

        assert usage is not None
        assert usage.input_tokens == 5
        assert usage.cost_microunits is None

    @pytest.mark.parametrize("token", ["Infinity", "-Infinity", "NaN"])
    def test_a_non_finite_cost_is_refused_rather_than_crashing(self, token: str) -> None:
        """``json.loads`` accepts these tokens and ``int(inf)`` raises. A hostile response must not
        crash a run — the P2-B lesson, re-applied on this transport."""
        body = f'{{"usage": {{"prompt_tokens": 5, "completion_tokens": 5, "cost": {token}}}}}'.encode()

        usage = extract_provider_usage(body, reported_by="p")

        assert usage is not None
        assert usage.cost_microunits is None

    def test_no_cost_field_leaves_the_cost_dimension_unmeasured(self) -> None:
        """Anthropic and OpenAI proper report no cost. Their tokens still meter; a cost cap on them
        needs an operator price table, and says so."""
        body = json.dumps({"usage": {"input_tokens": 9, "output_tokens": 3}}).encode()

        usage = extract_provider_usage(body, reported_by="p")

        assert usage is not None
        assert usage.input_tokens == 9
        assert usage.cost_microunits is None
