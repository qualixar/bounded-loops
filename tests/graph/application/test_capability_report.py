"""The capability report must describe the engine that exists, not the one the schema implies.

A host model reads this document instead of guessing. Every test here is a guard against the
report reading *better* than the code — an over-promise here becomes a graph the compiler refuses.
"""

from __future__ import annotations

import json

import pytest

from bounded_loops.graph.adapters.enforcement.capabilities import PlatformCapabilities
from bounded_loops.graph.adapters.enforcement.snapshot import platform_snapshot
from bounded_loops.graph.application.capability_report import capability_report
from bounded_loops.graph.application.refusals import REFUSALS
from bounded_loops.graph.domain.authoring import IsolationLevel, NodeKind

_BARE = PlatformCapabilities(
    platform="linux", docker_available=False, process_groups=True, rlimits=True,
)
_FULL = PlatformCapabilities(
    platform="linux", docker_available=True, process_groups=True, rlimits=True,
)


@pytest.fixture
def report() -> dict:
    return dict(capability_report(platform=platform_snapshot(capabilities=_BARE)))


def test_the_whole_document_survives_a_json_round_trip(report: dict) -> None:
    """It crosses an MCP boundary, so a non-serialisable value is a broken tool, not a warning."""
    assert json.loads(json.dumps(report)) == report


def test_every_node_kind_the_engine_defines_is_reported(report: dict) -> None:
    reported = [entry["kind"] for entry in report["node_kinds"]]
    assert reported == [kind.value for kind in NodeKind]


def test_kind_specific_fields_are_ACTUALLY_extracted_from_the_nested_schema(
    report: dict,
) -> None:
    """The schema nests each kind under oneOf[i].allOf[1], so a naive read finds nothing.

    That naive read reported all ten kinds as having no kind-specific fields — a confidently
    wrong answer that would tell a host model a loop node needs no loop_package. These four are
    spot-checks with known answers; the emptiness of ALL of them is what the last assert catches.
    """
    by_kind = {entry["kind"]: entry for entry in report["node_kinds"]}

    assert by_kind["loop"]["extra_required_fields"] == ["loop_package"]
    assert by_kind["tool"]["extra_required_fields"] == ["tool_ref"]
    assert by_kind["join"]["extra_required_fields"] == ["mode"]
    assert by_kind["router"]["extra_required_fields"] == ["routes"]
    assert "default_route" in by_kind["router"]["extra_optional_fields"]

    with_extras = [k for k, v in by_kind.items() if v["extra_required_fields"]]
    assert len(with_extras) >= 5, f"only {with_extras} report extras — the extraction regressed"


def test_the_refusal_table_is_carried_whole(report: dict) -> None:
    assert report["refusals"]["count"] == len(REFUSALS)
    assert sorted(report["refusals"]["codes"]) == sorted(REFUSALS)
    assert all(entry["fix"] for entry in report["refusals"]["table"])


def test_declared_and_honoured_failure_policies_are_reported_SEPARATELY(report: dict) -> None:
    policies = report["failure_policies"]
    assert set(policies["honoured"]) < set(policies["declared"])
    assert policies["refused"] == sorted(
        set(policies["declared"]) - set(policies["honoured"])
    )
    # The schema annotation and the validator constant must be the same answer.
    assert policies["schema_annotation"] == policies["refused"]


def test_isolation_reports_what_THIS_host_can_do_not_what_the_enum_lists(report: dict) -> None:
    tiers = {tier["level"]: tier for tier in report["isolation"]["tiers"]}
    assert set(tiers) == {level.value for level in IsolationLevel}
    # Injected: no container runtime, so this tier is undeliverable AND says why.
    assert tiers["container_restricted"]["deliverable_here"] is False
    assert tiers["container_restricted"]["reason_if_not"]
    assert tiers["workspace_only"]["deliverable_here"] is True


def test_a_tier_undeliverable_HERE_is_not_reported_as_undeliverable_EVERYWHERE() -> None:
    """"Unavailable on your laptop" and "unavailable in principle" are different facts.

    Conflating them would tell a host model that container isolation does not exist, when the
    honest statement is that this host cannot deliver it.
    """
    bare = dict(capability_report(platform=platform_snapshot(capabilities=_BARE)))
    full = dict(capability_report(platform=platform_snapshot(capabilities=_FULL)))

    bare_tiers = {t["level"]: t for t in bare["isolation"]["tiers"]}
    full_tiers = {t["level"]: t for t in full["isolation"]["tiers"]}

    assert bare_tiers["container_restricted"]["deliverable_here"] is False
    assert full_tiers["container_restricted"]["deliverable_here"] is True
    # `never_available` is a property of the engine, so it does NOT move with the host.
    assert bare["isolation"]["never_available"] == full["isolation"]["never_available"]
    assert "customer_managed_worker" in bare["isolation"]["never_available"]


def test_the_LOOP_and_GRAPH_status_vocabularies_are_reported_SEPARATELY(report: dict) -> None:
    """Merging them told a host that a graph run reaching SUCCEEDED was not success.

    A loop ends DONE/HALT/PAUSE/KILLED/ERROR. A graph run ends SUCCEEDED/FAILED/HALTED/
    CANCELLED/EXPIRED. Only DONE and only SUCCEEDED are success, and they are different words for
    different objects — a document that reports one set as "the" terminal statuses is wrong for
    whichever surface the reader is actually using.
    """
    loops = report["loop_statuses"]
    assert loops["success"] == ["DONE"]
    assert set(loops["not_success"]) == {"HALT", "PAUSE", "KILLED", "ERROR"}
    assert set(loops["all"]) == set(loops["success"]) | set(loops["not_success"])

    graphs = report["graph_run_states"]
    assert graphs["success"] == ["SUCCEEDED"]
    assert set(graphs["not_success"]) == {"FAILED", "HALTED", "CANCELLED", "EXPIRED"}
    # In-flight states are a THIRD answer: neither finished nor failed.
    assert set(graphs["non_terminal"]) == {"PENDING", "RUNNING"}
    assert "DONE" not in graphs["all"], "the loop vocabulary leaked into the graph one"
    assert "SUCCEEDED" not in loops["all"], "the graph vocabulary leaked into the loop one"

    assert "verbatim" in report["reporting_rule"]


def test_the_repair_contract_states_that_attempt_alone_is_not_an_identity(report: dict) -> None:
    """The 0.5.0 breaking change exists because of this; a host model must be told."""
    repair = report["repair"]
    assert repair["attempts_reset_at_a_boundary"] is True
    assert "repair_round" in repair["identity_warning"]
    assert repair["global_round_bound"] > 0


def test_every_budget_field_names_where_it_is_enforced(report: dict) -> None:
    """P0's rule: a budget the runtime ignores must not be advertised as a budget."""
    fields = {entry["field"]: entry for entry in report["budgets"]}
    assert set(fields) == {
        "max_attempts", "max_wallclock_s", "max_tokens", "max_cost_microunits",
    }
    for field, entry in fields.items():
        assert entry["enforced_by"], field
        assert entry["unit"], field


def test_the_gate_section_states_the_independence_rule(report: dict) -> None:
    """This is the one lesson a host model gets wrong by default."""
    gates = report["gates"]
    assert "DIFFERENT object" in gates["independence_rule"]
    assert "mechanical" in gates["independence_rule"]
    assert len(gates["kinds"]) >= 10
    assert all(entry["checks"] for entry in gates["kinds"])


# ── the report must not invent a control the host does not enforce ───────────


def test_the_reported_controls_come_from_the_ADAPTER_not_from_prose() -> None:
    """Mutation testing found this uncovered: appending a made-up control string to every tier
    left the suite green. The report could claim an undeliverable tier enforces something the
    host has never heard of, and a model choosing an isolation tier would believe it."""
    from bounded_loops.graph.adapters.enforcement.snapshot import platform_snapshot
    from bounded_loops.graph.application.capability_report import capability_report

    platform = platform_snapshot()
    document = capability_report(platform=platform)

    reported = {
        tier["level"]: tuple(tier["controls_enforced_here"])
        for tier in document["isolation"]["tiers"]
    }
    actual = {fact.level: tuple(fact.controls_enforced_here) for fact in platform.isolation}

    assert reported == actual, (
        "the capability document's controls do not match what the platform adapter reports"
    )


def test_a_tier_that_cannot_be_DELIVERED_here_claims_no_controls() -> None:
    """"We cannot run this" and "we enforce these controls" cannot both be true of one tier."""
    from bounded_loops.graph.adapters.enforcement.snapshot import platform_snapshot

    for fact in platform_snapshot().isolation:
        if not fact.deliverable_here:
            assert not fact.controls_enforced_here, (
                f"{fact.level} is not deliverable here yet claims to enforce "
                f"{fact.controls_enforced_here}"
            )


def test_seatbelt_is_probed_by_APPLYING_a_profile_not_by_checking_a_file_mode() -> None:
    """`os.access(X_OK)` is vacuously true inside a nested sandbox — a CI runner, a container,
    another agent's sandbox — where sandbox_apply then fails with "Operation not permitted".
    The report would promise `process_restricted` denies network and confines writes on a host
    where the profile cannot be installed at all."""
    import inspect

    from bounded_loops.graph.adapters.enforcement import capabilities

    source = inspect.getsource(capabilities._seatbelt_available)

    assert "subprocess.run" in source, (
        "the seatbelt probe no longer applies a profile; an executable bit is not enforcement"
    )
    assert "returncode == 0" in source
