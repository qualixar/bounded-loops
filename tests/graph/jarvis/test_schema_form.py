"""Forms derived from the compiler's schema, so the UI cannot offer what the compiler refuses."""

from __future__ import annotations

import json

import pytest

from bounded_loops.graph.jarvis import schema_form as SF


def test_every_node_kind_the_schema_defines_has_a_form() -> None:
    from bounded_loops.graph.domain.authoring import NodeKind

    assert set(SF.node_kinds()) == {kind.value for kind in NodeKind}


def test_a_kind_the_schema_does_not_define_RAISES_rather_than_returning_an_empty_form() -> None:
    """An empty form reads as "this kind needs nothing", which is the wrong answer to a typo."""
    with pytest.raises(KeyError, match="no node kind"):
        SF.node_form("wizardry")


def test_a_loop_node_form_carries_the_base_fields_AND_its_own() -> None:
    names = {field.name for field in SF.node_form("loop")}

    assert {"id", "kind", "inputs", "outputs", "budget", "effects", "isolation"} <= names
    assert "loop_package" in names, "the kind-specific field is missing"


def test_the_UI_CANNOT_offer_a_failure_policy_the_compiler_refuses() -> None:
    """This is the whole reason the forms are generated rather than written.

    `on_failure` is schema-valid for four values; the compiler refuses two of them. A hand-written
    dropdown offered all four, and every graph built with the refused ones failed at compile.
    """
    on_failure = next(f for f in SF.node_form("loop") if f.name == "on_failure")

    assert set(on_failure.offerable) == {"fail_graph", "repair"}
    refused = {c.value: c.reason for c in on_failure.choices if not c.available}
    assert set(refused) == {"continue", "await_human"}
    assert all(reason for reason in refused.values()), "a disabled choice with no reason is a mystery"


def test_the_UI_CANNOT_offer_an_isolation_tier_no_host_can_enforce() -> None:
    isolation = next(f for f in SF.node_form("loop") if f.name == "isolation")

    assert "customer_managed_worker" not in isolation.offerable
    disabled = next(c for c in isolation.choices if c.value == "customer_managed_worker")
    assert disabled.available is False
    assert "fails closed" in (disabled.reason or "")


def test_an_unavailable_choice_is_still_PRESENT_not_dropped() -> None:
    """Dropping it would leave a user who saw the value in our docs with no explanation."""
    on_failure = next(f for f in SF.node_form("loop") if f.name == "on_failure")

    assert {c.value for c in on_failure.choices} == {
        "fail_graph", "continue", "repair", "await_human",
    }


def test_the_budget_ref_EXPANDS_so_every_spend_ceiling_is_reachable() -> None:
    """The old Studio could not set max_tokens or max_cost_microunits. This is why.

    `budget` is a `$ref`; unresolved it renders as one opaque field and the four ceilings inside
    are unreachable from any generated form.
    """
    budget = next(f for f in SF.node_form("loop") if f.name == "budget")

    assert budget.kind == "object"
    sub = {field.name: field for field in budget.fields}
    assert set(sub) == {
        "max_attempts", "max_wallclock_s", "max_tokens", "max_cost_microunits",
    }
    assert sub["max_attempts"].required is True
    assert sub["max_attempts"].minimum == 1
    assert sub["max_attempts"].maximum == 100
    assert sub["max_wallclock_s"].maximum == 86400


def test_a_digest_field_carries_its_PATTERN_so_a_form_can_validate_before_sending() -> None:
    loop_package = next(f for f in SF.node_form("loop") if f.name == "loop_package")

    assert loop_package.kind == "string"
    assert loop_package.pattern == "^sha256:[0-9a-f]{64}$"


def test_the_graph_form_reaches_the_policy_fields_including_repair_budget() -> None:
    fields = {field.name: field for field in SF.graph_form()}

    assert {"graph_id", "version", "api_version"} <= set(fields)
    assert "repair_budget" in fields, "the global repair bound is unreachable from the UI"
    assert fields["repair_budget"].minimum == 0
    assert fields["repair_budget"].maximum == 100
    assert set(fields["fail_mode"].offerable) == {"fail_closed", "continue_declared"}


def test_the_edge_form_exposes_the_guard() -> None:
    names = {field.name for field in SF.edge_form()}

    assert {"from_node", "from_port", "to_node", "to_port", "when"} <= names


def test_the_whole_document_is_json_safe_and_covers_every_kind() -> None:
    document = SF.form_document()

    assert json.loads(json.dumps(document)) == json.loads(json.dumps(document))
    assert set(document["nodes"]) == set(SF.node_kinds())
    assert document["generated_from"].endswith("authoring-graph.schema.json")


def test_offerable_values_raises_for_a_field_that_does_not_exist() -> None:
    with pytest.raises(KeyError):
        SF.offerable_values(SF.node_form("loop"), "no_such_field")
