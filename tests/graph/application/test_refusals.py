"""The refusal table must not fall behind the validator that raises the refusals."""

from __future__ import annotations

import re
from pathlib import Path

from bounded_loops.graph.application.refusals import (
    REFUSAL_CODES,
    REFUSALS,
    explain,
)

_VALIDATOR = (
    Path(__file__).resolve().parents[3]
    / "bounded_loops" / "graph" / "application" / "validate_graph.py"
)


def _codes_raised_by_the_validator() -> frozenset[str]:
    """Every code the validator can actually raise, read out of its source.

    `re.S` matters: several `_error(` calls put the code on the line after the paren, and a
    line-oriented match silently misses them — which is how an earlier count came out at 34
    instead of 37.
    """
    source = _VALIDATOR.read_text(encoding="utf-8")
    return frozenset(re.findall(r'_error\(\s*"([a-z0-9_]+)"', source, re.S))


def test_the_table_documents_EVERY_refusal_the_validator_can_raise() -> None:
    raised = _codes_raised_by_the_validator()
    missing = raised - REFUSAL_CODES
    assert missing == frozenset(), (
        f"validate_graph.py raises {sorted(missing)} with no entry in refusals.py — a host model "
        "hitting one of these gets a code and no way to fix it"
    )


def test_the_table_documents_NOTHING_the_validator_cannot_raise() -> None:
    """A documented refusal that cannot happen teaches a host model to avoid a phantom."""
    raised = _codes_raised_by_the_validator()
    invented = REFUSAL_CODES - raised
    assert invented == frozenset(), f"refusals.py documents unreachable codes: {sorted(invented)}"


def test_the_extraction_actually_finds_something() -> None:
    """Guard against the guard: a broken regex would make both tests above pass vacuously."""
    raised = _codes_raised_by_the_validator()
    assert len(raised) >= 30, f"only {len(raised)} codes extracted — the regex is wrong"
    assert "on_failure_unimplemented" in raised, "the multi-line call form was missed again"


def test_every_entry_gives_a_fix_not_just_a_diagnosis() -> None:
    # Proven non-empty first: "every entry has a fix" is trivially true of no entries, and this
    # walks whatever the table holds.
    assert len(REFUSALS) >= 5, (
        f"only {len(REFUSALS)} refusal(s) in the table; a guard that iterates it passes on an "
        "empty table without reading a single message"
    )

    for code, refusal in REFUSALS.items():
        assert refusal.code == code, f"{code} keyed under the wrong code"
        assert refusal.summary.endswith("."), code
        assert len(refusal.fix) > 20, f"{code} has no actionable fix"
        assert "contact support" not in refusal.fix.lower(), code


def test_an_unknown_code_returns_None_rather_than_raising() -> None:
    """A crash inside error handling is the worst failure mode available."""
    assert explain("no_such_refusal_code") is None
    assert explain("cycle") is not None
