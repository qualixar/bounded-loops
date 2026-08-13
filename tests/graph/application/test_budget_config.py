"""Operator budget configuration: USD in, integer micro-USD stored, flags over file.

Money is entered in USD because that is what an invoice is denominated in, and stored as
integer micro-USD because replaying a run has to reproduce the same totals exactly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bounded_loops.graph.application.budget_config import (
    budget_from_mapping,
    describe,
    load_budget_file,
    resolve_run_budget,
    usd_to_microunits,
)
from bounded_loops.graph.application.node_spend import RunBudget
from bounded_loops.graph.domain.errors import GraphIntegrityError
from bounded_loops.graph.domain.pricing import empty_price_table


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0", 0), ("1", 1_000_000), ("2.50", 2_500_000), ("0.000001", 1),
        ("  10.25  ", 10_250_000), ("1000", 1_000_000_000),
    ],
)
def test_usd_converts_to_micro_usd_exactly(raw: str, expected: int) -> None:
    assert usd_to_microunits(raw) == expected


def test_the_conversion_does_not_go_through_a_float() -> None:
    """A float loses money on ordinary two-decimal amounts, so the path uses Decimal.

    1196 of the ~100_000 plain two-decimal USD amounts below $1000 convert wrong through a
    float — $2.01 among them, which becomes 2_009_999 micro-USD instead of 2_010_000. One
    micro-USD is not the point; the point is that the number then disagrees with itself
    between two readings of the same file, and a ceiling that is not reproducible is not a
    ceiling. Taking a STRING is what makes Decimal possible: a float argument would have lost
    the precision before this function could preserve it.
    """
    assert usd_to_microunits("2.01") == 2_010_000
    assert int(float("2.01") * 1_000_000) == 2_009_999  # the trap being avoided


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        ("-1", "non-negative"),
        ("not-money", "not a USD amount"),
        ("", "not a USD amount"),
        ("nan", "finite"),
        ("Infinity", "finite"),
        # Micro-USD is the smallest unit recorded, so a seventh decimal cannot be stored.
        # Refused rather than rounded: silently dropping a digit the operator typed makes the
        # budget mean something they did not write.
        ("1.0000001", "decimal places"),
    ],
)
def test_a_malformed_usd_amount_is_refused(raw: str, match: str) -> None:
    with pytest.raises(GraphIntegrityError, match=match):
        usd_to_microunits(raw)


def test_a_budget_file_carries_both_the_ceilings_and_the_rates(tmp_path: Path) -> None:
    """One file, because a cost ceiling without rates is unenforceable."""
    path = tmp_path / "budget.json"
    path.write_text(json.dumps({
        "max_tokens": 500_000,
        "max_cost_usd": "2.50",
        "prices": {"anthropic/claude-opus-5": {
            "input_microunits_per_mtok": 3_000_000, "output_microunits_per_mtok": 15_000_000,
        }},
    }), encoding="utf-8")

    budget, table = load_budget_file(path)

    assert budget.max_tokens == 500_000
    assert budget.max_cost_microunits == 2_500_000
    assert table.describes(provider_id="anthropic", model_id="claude-opus-5")
    assert table.source == "price-table:budget.json"


def test_a_budget_file_must_not_be_a_symlink(tmp_path: Path) -> None:
    """A symlink lets the file that was validated differ from the file that is read."""
    real = tmp_path / "real.json"
    real.write_text("{}", encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(real)

    with pytest.raises(GraphIntegrityError, match="symlink"):
        load_budget_file(link)


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        ("not-a-mapping", "must be an object"),
        ({"max_token": 5}, "unknown keys"),          # a misspelled ceiling reads as no ceiling
        ({"max_tokens": "500000"}, "must be an integer"),
        ({"max_tokens": True}, "must be an integer"),
        ({"max_cost_usd": "-1"}, "non-negative"),
    ],
)
def test_a_malformed_budget_document_is_refused(raw: object, match: str) -> None:
    with pytest.raises(GraphIntegrityError, match=match):
        budget_from_mapping(raw, source="price-table:test")


def test_an_absent_budget_file_means_no_ceiling_and_no_rates() -> None:
    budget, table = budget_from_mapping({}, source="price-table:test")

    assert not budget.declared
    assert table.prices == {}


@pytest.mark.parametrize(
    ("flag_tokens", "flag_cost", "expected_tokens", "expected_cost"),
    [
        # Neither flag: the file's numbers stand.
        (None, None, 500_000, 2_500_000),
        # One flag overrides ONLY its own dimension. Dropping the other would quietly remove a
        # bound the operator still expects to hold.
        (10, None, 10, 2_500_000),
        (None, "0.10", 500_000, 100_000),
        (10, "0.10", 10, 100_000),
    ],
)
def test_a_flag_overrides_the_file_per_dimension(
    flag_tokens, flag_cost, expected_tokens, expected_cost,
) -> None:
    from_file = RunBudget(max_tokens=500_000, max_cost_microunits=2_500_000)

    resolved = resolve_run_budget(
        from_file=from_file, max_tokens=flag_tokens, max_cost_usd=flag_cost,
    )

    assert resolved.max_tokens == expected_tokens
    assert resolved.max_cost_microunits == expected_cost


def test_a_zero_flag_is_an_override_not_an_absence() -> None:
    """``--max-cost-usd 0`` says "must not cost money", which is not the same as saying
    nothing. Testing truthiness instead of None would silently fall back to the file."""
    resolved = resolve_run_budget(
        from_file=RunBudget(max_tokens=500_000, max_cost_microunits=2_500_000),
        max_tokens=None, max_cost_usd="0",
    )

    assert resolved.max_cost_microunits == 0


def test_the_budget_is_describable_for_a_cli_or_a_ui_panel() -> None:
    budget, table = budget_from_mapping(
        {"max_tokens": 500_000, "max_cost_usd": "2.50",
         "prices": {"anthropic/claude-opus-5": {
             "input_microunits_per_mtok": 1, "output_microunits_per_mtok": 2}}},
        source="price-table:test",
    )

    body = describe(budget, table)

    assert body["max_tokens"] == 500_000
    assert body["max_cost_microunits"] == 2_500_000
    # The integer is what is enforced; the USD string sits alongside it for humans, never
    # instead of it.
    assert body["max_cost_usd"] == "2.5"
    assert body["priced_routes"] == ["anthropic/claude-opus-5"]
    assert body["price_table_source"] == "price-table:test"
    assert json.dumps(body)


def test_describing_an_undeclared_budget_omits_the_usd_line() -> None:
    body = describe(RunBudget(), empty_price_table())

    assert body["max_cost_microunits"] is None
    assert "max_cost_usd" not in body
    assert body["priced_routes"] == []
