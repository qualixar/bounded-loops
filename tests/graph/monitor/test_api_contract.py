"""What the monitor's API declares about itself must stay true.

Two auditors independently flagged `MUTATING_ROUTES` as a frozenset that looked
security-relevant and was read by nothing — the server never consulted it, so a fourth writer
could be added with no test, no warning, and a set that now quietly lied. It is read on every
dispatch now; this file is the other half, checking the declaration against reality.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bounded_loops.graph.monitor import api

#: Routes that write to disk or start work. Derived by reading each handler, not by copying
#: MUTATING_ROUTES — a test that restates the value it is checking proves nothing.
ROUTES_THAT_CHANGE_SOMETHING = {"graph.save", "approve", "execute"}


def test_every_route_that_changes_something_is_DECLARED_mutating() -> None:
    assert ROUTES_THAT_CHANGE_SOMETHING <= set(api.MUTATING_ROUTES), (
        "a route that changes state is missing from MUTATING_ROUTES: "
        f"{sorted(ROUTES_THAT_CHANGE_SOMETHING - set(api.MUTATING_ROUTES))}"
    )


def test_nothing_is_declared_mutating_that_does_not_change_anything() -> None:
    """Over-declaring is its own failure: a UI that warns about everything trains people to
    click through the warning that mattered."""
    assert set(api.MUTATING_ROUTES) <= ROUTES_THAT_CHANGE_SOMETHING, (
        "MUTATING_ROUTES names a route that does not write: "
        f"{sorted(set(api.MUTATING_ROUTES) - ROUTES_THAT_CHANGE_SOMETHING)}"
    )


def test_every_declared_route_actually_EXISTS() -> None:
    unknown = sorted(set(api.MUTATING_ROUTES) - set(api._ROUTES))
    assert not unknown, f"MUTATING_ROUTES names routes the server does not serve: {unknown}"


def test_the_response_tells_the_caller_whether_the_route_mutates() -> None:
    """The set is load-bearing now. If this stops being stamped it is inert again."""
    read_only = api.handle("capabilities", {})
    assert read_only["mutating"] is False

    unknown_route = api.handle("no-such-route", {})
    assert unknown_route["ok"] is False
    assert unknown_route["mutating"] is False


def test_a_mutating_route_says_so_even_when_it_REFUSES(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BOUNDED_LOOPS_WORKSPACE", str(tmp_path / "project"))

    refused = api.handle("graph.save", {"name": "ok-name", "manifest": "not: a graph\n"})

    assert refused["ok"] is False
    assert refused["mutating"] is True


# ── confirm must be a boolean, not merely truthy ─────────────────────────────


@pytest.mark.parametrize("value", ["false", "0", "no", 1, "true", [], {}, None, 0, ""])
def test_only_the_BOOLEAN_true_counts_as_confirmation(value: object) -> None:
    """`if not payload.get("confirm")` accepted any truthy JSON value, so the string "false"
    started a run. A client that stringifies its booleans — or a frontend bug — should not be
    able to turn a preview request into an execution."""
    assert api.requires_true_confirm(value) is False


def test_the_boolean_true_still_confirms() -> None:
    assert api.requires_true_confirm(True) is True


def test_a_string_false_does_not_START_a_run(tmp_path: Path, monkeypatch) -> None:
    """The end-to-end shape of the bug, on the route that actually executes work."""
    monkeypatch.setenv("BOUNDED_LOOPS_WORKSPACE", str(tmp_path / "project"))
    manifest = str(
        Path(__file__).resolve().parents[3] / "graphs" / "customer-data-request" / "graph.yaml"
    )

    result = api.handle("execute", {"manifest": manifest, "confirm": "false"})

    assert result.get("started") is not True, (
        f"the string 'false' started a run: {result}"
    )
