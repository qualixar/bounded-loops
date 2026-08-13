"""Turning operator budget configuration into a run budget and a price table.

Two sources, with a fixed precedence: a budget FILE supplies the deployment's standing
numbers, and an explicit CLI FLAG overrides them for this one run. That direction is the only
one that makes sense — a flag is typed deliberately for the run in front of you, a file was
written once and forgotten.

Money is entered in USD, because that is what an operator's invoice is denominated in, and
stored in integer micro-USD, because replaying a run has to reproduce the same totals exactly.
The conversion goes through ``Decimal``: ``float("0.1") * 1_000_000`` is 100000.00000000001,
and a budget that drifts in the eighth decimal place is a budget that disagrees with itself
across two readings of the same file.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from typing import Mapping

from bounded_loops.graph.application.node_spend import RunBudget
from bounded_loops.graph.domain.errors import GraphIntegrityError
from bounded_loops.graph.domain.pricing import (
    PriceTable,
    empty_price_table,
    price_table_from_mapping,
)
from bounded_loops.graph.domain.usage import MICROUNITS_PER_USD

#: Micro-USD is the finest unit stored, so six decimal places is the most a USD figure can
#: carry. More than that is refused rather than rounded: silently dropping a digit an operator
#: typed is how a budget ends up meaning something they did not write.
_MAX_USD_DECIMAL_PLACES = 6

_ALLOWED_KEYS = frozenset({"max_tokens", "max_cost_usd", "prices"})


def usd_to_microunits(raw: str) -> int:
    """``"2.50"`` to ``2_500_000`` micro-USD, exactly.

    Takes a STRING, not a float, so the value the operator typed is the value that is
    converted. Accepting a float here would mean the caller had already lost precision before
    this function could preserve any.
    """
    try:
        amount = Decimal(raw.strip())
    except (InvalidOperation, AttributeError) as exc:
        raise GraphIntegrityError(f"{raw!r} is not a USD amount") from exc
    if not amount.is_finite() or amount < 0:
        raise GraphIntegrityError("a USD budget must be a finite, non-negative amount")
    if -amount.as_tuple().exponent > _MAX_USD_DECIMAL_PLACES:  # type: ignore[operator]
        raise GraphIntegrityError(
            f"a USD budget carries at most {_MAX_USD_DECIMAL_PLACES} decimal places "
            "(micro-USD is the smallest unit recorded)"
        )
    return int(amount * MICROUNITS_PER_USD)


def load_budget_file(path: Path) -> tuple[RunBudget, PriceTable]:
    """Read a budget file: the run's ceilings and the rates that price it.

    Both live in one file on purpose. A cost ceiling without rates is unenforceable — every
    cost cap would fail closed as unmeasurable — so splitting them across two files would make
    the working configuration two files that must be kept in step.
    """
    if path.is_symlink():
        # Same rule as --inputs: a symlink lets the file that is validated differ from the
        # file that is read.
        raise GraphIntegrityError("a budget file must not be a symlink")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise GraphIntegrityError(f"cannot read budget file: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise GraphIntegrityError(f"budget file is not valid JSON: {exc}") from exc
    return budget_from_mapping(raw, source=f"price-table:{path.name}")


def budget_from_mapping(raw: object, *, source: str) -> tuple[RunBudget, PriceTable]:
    """Parse an already-decoded budget document."""
    if not isinstance(raw, Mapping) or not all(isinstance(key, str) for key in raw):
        raise GraphIntegrityError("a budget file must be an object with string keys")
    unknown = set(raw) - _ALLOWED_KEYS
    if unknown:
        # Closed, so a misspelled "max_token" is reported rather than ignored — an ignored
        # ceiling reads exactly like no ceiling at all.
        raise GraphIntegrityError(f"budget file has unknown keys: {sorted(unknown)}")
    max_tokens = raw.get("max_tokens")
    if max_tokens is not None and (isinstance(max_tokens, bool) or not isinstance(max_tokens, int)):
        raise GraphIntegrityError("budget file max_tokens must be an integer")
    cost = raw.get("max_cost_usd")
    max_cost_microunits = None
    if cost is not None:
        # str(cost) rather than a float path: a JSON number like 2.5 has already been parsed
        # by json, but Decimal(str(2.5)) is exactly Decimal("2.5"), so the round trip is safe
        # for the decimal literals an operator actually writes.
        max_cost_microunits = usd_to_microunits(str(cost))
    prices = raw.get("prices")
    table = (
        price_table_from_mapping(prices, source=source) if prices is not None
        else empty_price_table()
    )
    return (
        RunBudget(max_tokens=max_tokens, max_cost_microunits=max_cost_microunits),
        table,
    )


def resolve_run_budget(
    *, from_file: RunBudget, max_tokens: int | None, max_cost_usd: str | None,
) -> RunBudget:
    """Apply CLI overrides over the file's numbers, per dimension.

    Per dimension, not whole-value: an operator who sets a token ceiling on the command line
    has said nothing about the cost ceiling in their file, and dropping it would quietly remove
    a bound they still expect to hold.
    """
    return RunBudget(
        max_tokens=from_file.max_tokens if max_tokens is None else max_tokens,
        max_cost_microunits=(
            from_file.max_cost_microunits if max_cost_usd is None
            else usd_to_microunits(max_cost_usd)
        ),
    )


def describe(budget: RunBudget, table: PriceTable) -> dict[str, object]:
    """The budget as a caller would show it — CLI ``--json``, a UI panel, a status line."""
    body: dict[str, object] = {
        "max_tokens": budget.max_tokens,
        "max_cost_microunits": budget.max_cost_microunits,
        "priced_routes": sorted(f"{provider}/{model}" for provider, model in table.prices),
        "price_table_source": table.source,
    }
    if budget.max_cost_microunits is not None:
        # Shown alongside the integer, never instead of it: the integer is what is enforced.
        body["max_cost_usd"] = str(
            Decimal(budget.max_cost_microunits) / MICROUNITS_PER_USD
        )
    return body
