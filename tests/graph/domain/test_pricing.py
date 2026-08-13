"""Tokens become money in integer micro-USD, from rates the OPERATOR supplies.

No default prices ship. That is the load-bearing decision here: a table baked into the
package is wrong the week a provider reprices, and a stale low price under-charges — which
is the direction that lets a cost cap permit more spend than was authorised.
"""

from __future__ import annotations

import pytest

from bounded_loops.graph.domain.errors import GraphIntegrityError
from bounded_loops.graph.domain.pricing import (
    ModelPrice,
    PriceTable,
    empty_price_table,
    price_table_from_mapping,
)

# $3 per million input tokens, $15 per million output — Opus-class list pricing, in micro-USD.
_OPUS = ModelPrice(input_microunits_per_mtok=3_000_000, output_microunits_per_mtok=15_000_000)


def _table() -> PriceTable:
    return PriceTable(prices={("anthropic", "claude-opus-5"): _OPUS}, source="price-table:test")


def test_a_charge_is_input_plus_output_at_their_own_rates() -> None:
    assert _OPUS.cost_microunits(input_tokens=1_000, output_tokens=500) == 3_000 + 7_500


def test_a_fractional_charge_rounds_up_never_down() -> None:
    """Truncation under-charges, and under-charging a cap lets spend past it.

    Sub-micro-USD is a rounding artefact whichever way it goes, so the direction is picked
    to be safe rather than precise.
    """
    half = ModelPrice(input_microunits_per_mtok=3_500_000, output_microunits_per_mtok=0)

    assert half.cost_microunits(input_tokens=1, output_tokens=0) == 4  # 3.5 exact
    assert half.cost_microunits(input_tokens=3, output_tokens=0) == 11  # 10.5 exact
    assert half.cost_microunits(input_tokens=2, output_tokens=0) == 7  # 7.0 exact, unchanged


def test_a_deployment_prices_nothing_until_its_operator_says_otherwise() -> None:
    """The default state. Every cost cap is then unmeasurable and fails closed."""
    table = empty_price_table()

    assert table.cost_microunits(
        provider_id="anthropic", model_id="claude-opus-5",
        input_tokens=1_000, output_tokens=500,
    ) is None
    assert not table.describes(provider_id="anthropic", model_id="claude-opus-5")


@pytest.mark.parametrize(
    ("provider_id", "model_id", "input_tokens", "output_tokens"),
    [
        ("openai", "gpt-5", 1_000, 500),          # route not in the table
        ("anthropic", "claude-opus-4", 1_000, 500),  # right provider, unpriced model
        (None, "claude-opus-5", 1_000, 500),      # node is not routed at all
        ("anthropic", "claude-opus-5", 1_000, None),  # output tokens unmeasured
        ("anthropic", "claude-opus-5", None, 500),    # input tokens unmeasured
    ],
)
def test_an_uncomputable_charge_is_none_and_never_zero(
    provider_id, model_id, input_tokens, output_tokens,
) -> None:
    """Zero would read as "this was free", which is a claim, not an absence of one."""
    assert _table().cost_microunits(
        provider_id=provider_id, model_id=model_id,
        input_tokens=input_tokens, output_tokens=output_tokens,
    ) is None


def test_a_model_id_containing_slashes_survives_the_key_split() -> None:
    """OpenRouter names models `vendor/model`, so only the FIRST slash separates provider."""
    table = price_table_from_mapping(
        {"openrouter/anthropic/claude-opus-4": {
            "input_microunits_per_mtok": 3_000_000, "output_microunits_per_mtok": 15_000_000,
        }},
        source="price-table:test",
    )

    assert table.describes(provider_id="openrouter", model_id="anthropic/claude-opus-4")


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        ("not-a-mapping", "must be an object"),
        ({"noslash": {"input_microunits_per_mtok": 1, "output_microunits_per_mtok": 1}},
         "provider/model"),
        ({"/model": {"input_microunits_per_mtok": 1, "output_microunits_per_mtok": 1}},
         "must name both"),
        ({"p/m": "not-an-object"}, "must be an object"),
        ({"p/m": {"input_microunits_per_mtok": 1}}, "missing"),
        ({"p/m": {"input_microunits_per_mtok": 1, "output_microunits_per_mtok": 1, "x": 2}},
         "unknown keys"),
        ({"p/m": {"input_microunits_per_mtok": 1.5, "output_microunits_per_mtok": 1}},
         "must be an integer"),
        ({"p/m": {"input_microunits_per_mtok": -1, "output_microunits_per_mtok": 1}},
         "non-negative"),
    ],
)
def test_a_malformed_price_table_is_refused(raw, match) -> None:
    with pytest.raises(GraphIntegrityError, match=match):
        price_table_from_mapping(raw, source="price-table:test")


def test_a_missing_output_rate_is_refused_rather_than_defaulted_to_free() -> None:
    """Output tokens are the expensive side; defaulting them to 0 would read as a real rate."""
    with pytest.raises(GraphIntegrityError, match="output_microunits_per_mtok"):
        price_table_from_mapping(
            {"anthropic/claude-opus-5": {"input_microunits_per_mtok": 3_000_000}},
            source="price-table:test",
        )
