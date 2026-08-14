"""Tripwires for constants that are deliberately duplicated across layers.

Each pair below is mirrored rather than shared, because importing across the boundary
would create a dependency the architecture does not want. The cost of mirroring is drift:
someone edits one and not the other, and the two layers disagree silently. These tests
are the cheap alarm for that.
"""

from __future__ import annotations

import json
from pathlib import Path

from bounded_loops.graph.adapters.connectors.local_cli_worker import CLI_PROFILES
from bounded_loops.graph.adapters.persistence.event_log import _NODE_EVENTS
from bounded_loops.graph.application.arena_projection import _ALLOWED, _LIFECYCLE_EVENTS
from bounded_loops.graph.application.node_spend import EFFECTFUL_EFFECTS
from bounded_loops.graph.application.node_spend import MAX_ATTEMPTS_CEILING as CONTROLLER_CEILING
from bounded_loops.graph.application.validate_graph import (
    _ON_FAILURE_DECLARED,
    _ON_FAILURE_UNIMPLEMENTED,
    _PROVIDERS,
)
from bounded_loops.graph.application.validate_graph import _MAX_ATTEMPTS_CEILING as SCHEMA_CEILING
from bounded_loops.graph.domain.authoring import NETWORK_EFFECTS, Effect


def _schema() -> dict:
    """The schema as shipped on disk — deliberately not via `authoring_graph_schema()`.

    These tests guard the published file that integrators validate against, so they read the
    file rather than any loader that could normalise or default something away.
    """
    return json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "bounded_loops" / "graph" / "schemas" / "authoring-graph.schema.json"
        ).read_text(encoding="utf-8")
    )


def test_the_retry_ceiling_agrees_between_the_validator_and_the_controller() -> None:
    """A validator ceiling above the controller's would admit a plan the run then refuses."""
    assert SCHEMA_CEILING == CONTROLLER_CEILING


def test_the_json_schema_retry_ceiling_agrees_with_the_code() -> None:
    """The published schema is what integrators validate against before ever running us."""
    budget = _schema()["$defs"]["budget"]["properties"]["max_attempts"]
    assert budget["maximum"] == SCHEMA_CEILING


def test_the_schema_declares_which_failure_policies_the_compiler_REFUSES() -> None:
    """A schema that advertises what the compiler rejects generates invalid work by design.

    `on_failure`'s enum includes `continue` and `await_human` so that an existing manifest
    using them still reaches the validator's good refusal message rather than an opaque
    schema error. But anything generating an authoring UI from this schema — which is exactly
    what the Jarvis config forms do — would offer them as choices, and every graph a
    non-technical user built with them would be refused at compile.

    `x-unimplemented` is the machine-readable answer, mirrored from the validator rather than
    imported (the schema is data shipped to integrators, not code). This is the alarm for that
    mirroring: implementing one of these means removing it from BOTH.
    """
    schema = _schema()
    annotated = schema["$defs"]["baseNode"]["properties"]["on_failure"]["x-unimplemented"]
    assert frozenset(annotated) == _ON_FAILURE_UNIMPLEMENTED
    declared = schema["$defs"]["baseNode"]["properties"]["on_failure"]["oneOf"][0]["enum"]
    assert frozenset(declared) == _ON_FAILURE_DECLARED, (
        "the schema enum and the validator's declared set must still agree"
    )
    assert _ON_FAILURE_UNIMPLEMENTED < _ON_FAILURE_DECLARED, (
        "refusing a value the validator does not even declare is unreachable code"
    )


def test_the_schema_declares_which_isolation_TIERS_can_never_be_enforced() -> None:
    """`customer_managed_worker` is schema-valid and unconditionally unavailable.

    `PlatformCapabilities.select_mechanism` returns `(None, "no admitted customer-managed
    worker transport is available")` for it on every platform, with no probe involved — so a
    graph naming that tier fails closed everywhere, always. An authoring UI must not offer it.
    """
    from bounded_loops.graph.adapters.enforcement.capabilities import PlatformCapabilities
    from bounded_loops.graph.application.execution_policy import NetworkMode
    from bounded_loops.graph.domain.authoring import IsolationLevel

    schema = _schema()
    isolation = schema["$defs"]["baseNode"]["properties"]["isolation"]
    annotated = frozenset(isolation["x-never-available"])

    assert frozenset(isolation["enum"]) == {level.value for level in IsolationLevel}

    # Derived, not asserted from a literal: a tier is "never available" when the most capable
    # platform we can describe still cannot deliver it.
    fully_capable = PlatformCapabilities(
        platform="linux", docker_available=True, process_groups=True, rlimits=True,
    )
    never = frozenset(
        level.value
        for level in IsolationLevel
        if not fully_capable.can_enforce(level, NetworkMode.DENY)[0]
    )
    assert annotated == never, (
        f"schema says {sorted(annotated)} can never be enforced; capabilities says {sorted(never)}"
    )


def test_the_projection_knows_exactly_the_lifecycle_events_the_log_defines() -> None:
    """The projection reads ``state`` from these events and nothing else carries one.

    It previously selected them by the ``node.`` prefix, which also matched the additive
    ``node.attempt.failed`` — an event with no ``state`` — and raised KeyError on every
    retried run. Selecting an explicit set fixed that, at the cost of this drift risk: a
    new lifecycle event added to the log alone would be silently ignored by the Arena and
    the resume path.
    """
    assert _LIFECYCLE_EVENTS == frozenset(_NODE_EVENTS)


def test_every_lifecycle_state_has_a_transition_rule() -> None:
    """A state reachable by an event but absent from ``_ALLOWED`` raises KeyError on read."""
    assert set(_NODE_EVENTS.values()) <= set(_ALLOWED)


def test_the_portability_denylist_covers_every_shipped_provider() -> None:
    """A slot may declare capabilities, never providers — for every provider we ship.

    ``_PROVIDERS`` is a denylist enforcing portability: naming a provider in a slot's
    ``requires`` pins the graph to one vendor. A provider the project ships a CLI profile
    for but which is missing from the denylist can be named in a slot and pass validation,
    silently defeating the rule for exactly the providers most likely to be named.

    Mirrored rather than imported because the denylist lives in the application layer and
    the profiles in an adapter; this test is the alarm for the drift that mirroring allows.
    """
    assert set(CLI_PROFILES) <= _PROVIDERS


def test_adding_an_effect_forces_a_retry_safety_decision() -> None:
    """``EFFECTFUL_EFFECTS`` aliases ``NETWORK_EFFECTS``, which conflates two axes.

    Retry safety and network posture happen to select the same three effects today, but they
    are different questions. Aliasing them means a new network-bearing effect that IS safe to
    retry would be silently barred from retrying, and a new retry-UNSAFE effect that carries
    no network would be silently allowed to retry — the second direction being the dangerous
    one, since that is the double-spend this guard exists to prevent.

    Pinning the enum makes adding any effect fail here, forcing the author to decide which
    axis it belongs to instead of inheriting one by accident.
    """
    assert {effect.value for effect in Effect} == {
        "read_only", "workspace_write", "external_write", "financial", "irreversible",
    }
    assert EFFECTFUL_EFFECTS == NETWORK_EFFECTS
    assert NETWORK_EFFECTS == frozenset(
        {Effect.EXTERNAL_WRITE, Effect.FINANCIAL, Effect.IRREVERSIBLE}
    )
