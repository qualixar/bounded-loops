"""Turning token counts into money, in integer micro-USD.

Providers report TOKENS, not cost — OpenAI, Anthropic and Gemini all do. So a cost cap is
unusable without a price, and this is where the price comes from.

NO DEFAULT PRICES SHIP. That is a decision, not an omission:

* A table baked into the package is wrong the week a provider reprices, and its error is in
  the unsafe direction — a stale low price under-charges, so a cost cap silently permits more
  spend than the operator authorised.
* Enterprise rates are not list rates. Any deployment that cares about a cost cap already
  knows its own contracted numbers, and a shipped guess would quietly override them.

So an unpriced route makes cost UNMEASURABLE, and a node declaring a cost cap on it fails
closed naming the route to price. The same rule as everywhere else here: refuse rather than
guess at a bound.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from bounded_loops.graph.domain.errors import GraphIntegrityError

#: Providers publish prices per million tokens, so that is the unit stored — converting at
#: authoring time would force a fractional per-token figure and reintroduce floats.
TOKENS_PER_PRICE_UNIT = 1_000_000


@dataclass(frozen=True)
class ModelPrice:
    """Micro-USD per million tokens, input and output priced separately."""

    input_microunits_per_mtok: int
    output_microunits_per_mtok: int

    def __post_init__(self) -> None:
        for field in ("input_microunits_per_mtok", "output_microunits_per_mtok"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise GraphIntegrityError(f"price {field} must be a non-negative integer")

    def cost_microunits(self, *, input_tokens: int, output_tokens: int) -> int:
        """The charge for one call, rounded UP to the next whole micro-USD.

        Rounding up, not down. Integer division would truncate, and truncation under-charges;
        under-charging a cap lets spend past it. Sub-micro-USD is a rounding artefact either
        way, so the direction is chosen to be safe rather than precise.
        """
        exact = (
            input_tokens * self.input_microunits_per_mtok
            + output_tokens * self.output_microunits_per_mtok
        )
        return -(-exact // TOKENS_PER_PRICE_UNIT)


@dataclass(frozen=True)
class PriceTable:
    """Operator-supplied prices, keyed by ``(provider_id, model_id)``.

    The identity is the route's provider and model as the immutable plan records them, so a
    price cannot be attached to a route the run did not actually take.
    """

    prices: Mapping[tuple[str, str], ModelPrice]
    #: Names this table in receipts, so an estimated charge can be audited back to the exact
    #: rates that produced it. A file path or a version tag — never a bare "estimate".
    source: str = "price-table"

    def cost_microunits(
        self, *, provider_id: str | None, model_id: str | None,
        input_tokens: int | None, output_tokens: int | None,
    ) -> int | None:
        """The estimated charge, or ``None`` when it cannot be computed honestly.

        ``None`` for an unpriced route, an unrouted node, or half-measured tokens. Each of
        those is a real gap, and returning 0 for any of them would read as "this was free".
        """
        if provider_id is None or model_id is None:
            return None
        if input_tokens is None or output_tokens is None:
            return None
        price = self.prices.get((provider_id, model_id))
        if price is None:
            return None
        return price.cost_microunits(input_tokens=input_tokens, output_tokens=output_tokens)

    def describes(self, *, provider_id: str | None, model_id: str | None) -> bool:
        """Whether this table can price that route at all."""
        if provider_id is None or model_id is None:
            return False
        return (provider_id, model_id) in self.prices


def empty_price_table() -> PriceTable:
    """A table that prices nothing, which is the default state of a deployment.

    Every cost cap is then unmeasurable and fails closed, which is the intended behaviour:
    an operator who wants cost bounds supplies the rates their contract actually says.
    """
    return PriceTable(prices={}, source="price-table:none")


def price_table_from_mapping(raw: object, *, source: str) -> PriceTable:
    """Build a table from operator configuration.

    Shape: ``{"provider/model": {"input_microunits_per_mtok": N, "output_...": N}}``. The key
    splits on the FIRST ``/`` only, so a model id that itself contains slashes survives
    intact: ``openrouter/anthropic/claude-opus-4`` is provider ``openrouter`` and model
    ``anthropic/claude-opus-4``, which is exactly how OpenRouter names it.
    """
    if not isinstance(raw, Mapping):
        raise GraphIntegrityError("a price table must be an object")
    prices: dict[tuple[str, str], ModelPrice] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or "/" not in key:
            raise GraphIntegrityError(
                f"price table key {key!r} must be 'provider/model'"
            )
        provider_id, model_id = key.split("/", 1)
        if not provider_id or not model_id:
            raise GraphIntegrityError(f"price table key {key!r} must name both provider and model")
        if not isinstance(value, Mapping):
            raise GraphIntegrityError(f"price for {key!r} must be an object")
        unknown = set(value) - {"input_microunits_per_mtok", "output_microunits_per_mtok"}
        if unknown:
            raise GraphIntegrityError(f"price for {key!r} has unknown keys: {sorted(unknown)}")
        missing = {"input_microunits_per_mtok", "output_microunits_per_mtok"} - set(value)
        if missing:
            # Defaulting a missing side to 0 would price output tokens — the expensive
            # side — as free, and read as a deliberate rate rather than a typo.
            raise GraphIntegrityError(f"price for {key!r} is missing: {sorted(missing)}")
        prices[(provider_id, model_id)] = ModelPrice(
            input_microunits_per_mtok=_price_field(value, "input_microunits_per_mtok", key),
            output_microunits_per_mtok=_price_field(value, "output_microunits_per_mtok", key),
        )
    return PriceTable(prices=prices, source=source)


def _price_field(value: Mapping[str, object], field: str, key: str) -> int:
    raw = value[field]
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise GraphIntegrityError(f"price for {key!r}: {field} must be an integer")
    return raw
