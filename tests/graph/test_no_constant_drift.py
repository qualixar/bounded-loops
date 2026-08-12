"""Tripwires for constants that are deliberately duplicated across layers.

Each pair below is mirrored rather than shared, because importing across the boundary
would create a dependency the architecture does not want. The cost of mirroring is drift:
someone edits one and not the other, and the two layers disagree silently. These tests
are the cheap alarm for that.
"""

from __future__ import annotations

import json
from pathlib import Path

from bounded_loops.graph.adapters.persistence.event_log import _NODE_EVENTS
from bounded_loops.graph.application.arena_projection import _ALLOWED, _LIFECYCLE_EVENTS
from bounded_loops.graph.application.run_graph import _EFFECTFUL_EFFECTS
from bounded_loops.graph.application.run_graph import _MAX_ATTEMPTS_CEILING as CONTROLLER_CEILING
from bounded_loops.graph.application.validate_graph import _MAX_ATTEMPTS_CEILING as SCHEMA_CEILING
from bounded_loops.graph.domain.authoring import NETWORK_EFFECTS, Effect


def test_the_retry_ceiling_agrees_between_the_validator_and_the_controller() -> None:
    """A validator ceiling above the controller's would admit a plan the run then refuses."""
    assert SCHEMA_CEILING == CONTROLLER_CEILING


def test_the_json_schema_retry_ceiling_agrees_with_the_code() -> None:
    """The published schema is what integrators validate against before ever running us."""
    schema = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "bounded_loops" / "graph" / "schemas" / "authoring-graph.schema.json"
        ).read_text(encoding="utf-8")
    )
    budget = schema["$defs"]["budget"]["properties"]["max_attempts"]
    assert budget["maximum"] == SCHEMA_CEILING


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


def test_adding_an_effect_forces_a_retry_safety_decision() -> None:
    """``_EFFECTFUL_EFFECTS`` aliases ``NETWORK_EFFECTS``, which conflates two axes.

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
    assert _EFFECTFUL_EFFECTS == NETWORK_EFFECTS
    assert NETWORK_EFFECTS == frozenset(
        {Effect.EXTERNAL_WRITE, Effect.FINANCIAL, Effect.IRREVERSIBLE}
    )
