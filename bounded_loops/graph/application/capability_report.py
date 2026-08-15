"""The honest capability document: what this engine can do, and what it only declares.

This is the document a host model reads instead of guessing. It exists because the failure mode
of an orchestrator that does not know its tool is not an error message — it is a confident,
well-formed graph the compiler refuses, produced over and over.

Two rules govern every entry:

1. **Declared is not honoured.** Where the schema accepts a value the runtime does not route,
   or an isolation tier no platform can deliver, this report says so explicitly. A capability
   list that reads better than the code is a lie with good manners.
2. **Here is not everywhere.** Gate availability and isolation enforcement depend on the machine.
   The report carries a `platform` block describing THIS host, so a host model does not propose
   `container_restricted` on a box with no container runtime.

Pure application logic: no MCP, no CLI, no HTTP. The MCP tool, the `bl` command, the monitor UI,
and the docs generator are all thin readers of this one function — the alternative is four
capability lists free to disagree.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from bounded_loops import __version__
from bounded_loops.graph.application.slm_bridge import contract_advertisement
from bounded_loops.application.introspection import list_gates
from bounded_loops.domain.models import Status
from bounded_loops.graph.application.refusals import REFUSALS
from bounded_loops.graph.application.schemas import authoring_graph_schema
from bounded_loops.graph.application.validate_graph import (
    _API_VERSION,
    _MAX_ATTEMPTS_CEILING,
    _MAX_REPAIR_ROUNDS,
    _ON_FAILURE_DECLARED,
    _ON_FAILURE_UNIMPLEMENTED,
)
from bounded_loops.graph.domain.authoring import (
    NETWORK_EFFECTS,
    DataClass,
    Effect,
    NodeKind,
)

# Statuses that are NOT success. Stated as a set rather than "anything but DONE" because the
# whole point of the contract is that a caller must not treat a non-DONE run as finished work,
# and an explicit list survives a new status being added better than a negation does.
_NON_SUCCESS_STATUSES = frozenset(
    status.value for status in Status if status is not Status.DONE
)

#: A GRAPH run's terminal states, and the one that means success. Mirrored from
#: `adapters.persistence.event_log._TERMINAL` rather than imported, because the application layer
#: may not name a concrete adapter — `tests/graph/test_no_constant_drift.py` is the alarm for the
#: drift that mirroring allows, and it fails if the two ever disagree.
GRAPH_TERMINAL_STATES = frozenset({"SUCCEEDED", "FAILED", "HALTED", "CANCELLED", "EXPIRED"})
_GRAPH_SUCCESS = "SUCCEEDED"

#: Every run-level state, terminal or not. `PENDING` and `RUNNING` are in flight: a run in either
#: is neither finished nor failed, which is a third answer a caller has to handle.
GRAPH_RUN_STATES = GRAPH_TERMINAL_STATES | frozenset({"PENDING", "RUNNING"})


@dataclass(frozen=True)
class IsolationFact:
    """What one isolation tier delivers, as already determined by the enforcement adapter.

    `deliverable_here` and `available_anywhere` are different questions on purpose. "Your host
    has no container runtime" and "no host can ever deliver this tier" are different facts, and
    reporting the first as the second would tell a host model that container isolation does not
    exist.
    """

    level: str
    deliverable_here: bool
    reason_if_not: str | None
    controls_enforced_here: tuple[str, ...]
    available_anywhere: bool


@dataclass(frozen=True)
class PlatformSnapshot:
    """Everything this report needs to know about a host, already measured.

    This exists so the report is pure application logic: it formats facts, it does not probe for
    them. The enforcement adapter owns probing (see
    `bounded_loops.graph.adapters.enforcement.snapshot.platform_snapshot`), and the entry point —
    CLI, MCP, UI — hands the result in. The repo's import-graph test enforces that direction:
    an application module that names a concrete adapter cannot be re-wired for a different
    deployment without editing it.
    """

    platform: str
    container_runtime_reachable: bool
    process_groups: bool
    rlimits: bool
    isolation: tuple[IsolationFact, ...]


def capability_report(*, platform: PlatformSnapshot) -> Mapping[str, Any]:
    """The full capability document for the host described by `platform`."""
    schema = authoring_graph_schema()

    return {
        "engine": {
            "version": __version__,
            "graph_api_version": _API_VERSION,
            "what_it_is": (
                "A bounded loop is one task driven to a verified finish: a worker attempts, an "
                "INDEPENDENT gate decides, and it retries to a hard attempt bound. A graph is a "
                "DAG of those loops. The only durable state is an append-only hash-chained "
                "receipt log."
            ),
        },
        # What another product may rely on, and the axis it should branch on. `engine.version`
        # above is provenance — it says which build produced a document, not what a consumer
        # is entitled to expect. A consumer that pins our semver breaks on every release; one
        # that branches on the contract id keeps working across them, which is the whole point.
        "evidence_contracts": [contract_advertisement()],
        "node_kinds": _node_kinds(schema),
        "gates": _gates(),
        "isolation": _isolation(platform),
        "failure_policies": _failure_policies(schema),
        "repair": {
            "how": (
                "on_failure: {mode: repair, target: <ancestor>} sends the run back to an "
                "ancestor node. That boundary is a repair ROUND."
            ),
            "global_round_bound": _MAX_REPAIR_ROUNDS,
            "requires": "policies.repair_budget > 0 — without it the graph is refused",
            "attempts_reset_at_a_boundary": True,
            "identity_warning": (
                "Because attempts reset at a repair boundary, `attempt` alone is NOT an identity. "
                "Anything keyed per try must carry (attempt, repair_round)."
            ),
            "unreachable_under": "fail_mode: fail_closed — the run stops at the first failure",
        },
        "effects": _effects(),
        "budgets": _budgets(),
        # TWO vocabularies, reported separately. They were merged into one block that listed only
        # the LOOP statuses, so the document told a host that a graph run reaching SUCCEEDED — its
        # actual success state — was not success. A capability document that misnames the success
        # condition is worse than one that omits it.
        "loop_statuses": {
            "all": [status.value for status in Status],
            "success": [Status.DONE.value],
            "not_success": sorted(_NON_SUCCESS_STATUSES),
            "contract": (
                "A LOOP run (`bl run`, `bl_run`) ends in one of these. Only DONE means the gate "
                "passed and any required approval was granted."
            ),
        },
        "graph_run_states": {
            "all": sorted(GRAPH_RUN_STATES),
            "success": [_GRAPH_SUCCESS],
            "not_success": sorted(GRAPH_TERMINAL_STATES - {_GRAPH_SUCCESS}),
            "non_terminal": sorted(GRAPH_RUN_STATES - GRAPH_TERMINAL_STATES),
            "contract": (
                "A GRAPH run (`bl graph run`, `graph_status`) ends in one of the terminal states. "
                "Only SUCCEEDED is success. A state that is not terminal at all means the run is "
                "still in flight — neither finished nor failed."
            ),
        },
        "reporting_rule": (
            "Report the status the engine returned, verbatim. Every non-success status is "
            "unfinished work and must never be described as partial success — an ERROR run has no "
            "verdict at all, because the gate never returned one."
        ),
        "data_classes": [item.value for item in DataClass],
        "refusals": {
            "count": len(REFUSALS),
            "codes": sorted(REFUSALS),
            "table": [
                {"code": r.code, "summary": r.summary, "fix": r.fix}
                for r in sorted(REFUSALS.values(), key=lambda item: item.code)
            ],
        },
        "platform": {
            "platform": platform.platform,
            "container_runtime_reachable": platform.container_runtime_reachable,
            "process_groups": platform.process_groups,
            "rlimits": platform.rlimits,
            "caveat": (
                "Describes THIS host. A graph that compiles here can still fail closed on a "
                "host with fewer capabilities — that is the intended behaviour, not a bug."
            ),
        },
    }


# ── sections ─────────────────────────────────────────────────────────────────


def _node_kinds(schema: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every node kind with the fields the schema adds beyond the base node.

    Both required and optional extras are reported: a form generator needs the optional ones
    (a router's `default_route`) as much as the required ones, and a host model authoring a
    node needs to know an optional field exists before it can use it.
    """
    base_required = set(schema["$defs"]["baseNode"].get("required", ()))
    per_kind: dict[str, tuple[list[str], list[str]]] = {}
    for variant in schema["$defs"]["node"].get("oneOf", ()):
        properties, required = _variant_shape(variant)
        kind = _pinned_kind(properties)
        if kind is None:
            continue
        extra_required = sorted(set(required) - base_required - {"kind"})
        extra_optional = sorted(set(properties) - set(required) - base_required - {"kind"})
        per_kind[kind] = (extra_required, extra_optional)

    return [
        {
            "kind": kind.value,
            "extra_required_fields": per_kind.get(kind.value, ([], []))[0],
            "extra_optional_fields": per_kind.get(kind.value, ([], []))[1],
        }
        for kind in NodeKind
    ]


def _variant_shape(variant: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Flatten one oneOf variant's own properties/required with those of its allOf members.

    The schema writes each kind as `oneOf[i].allOf = [{$ref: baseNode}, {the kind's own bits}]`,
    so reading `variant["properties"]` directly finds nothing — which reported every kind as
    having no kind-specific fields at all, a confidently wrong answer. `$ref` members are
    skipped: the base node is subtracted by the caller, not merged in here.
    """
    properties: dict[str, Any] = {}
    required: list[str] = []
    for member in (variant, *variant.get("allOf", ())):
        if not isinstance(member, Mapping) or "$ref" in member:
            continue
        member_properties = member.get("properties")
        if isinstance(member_properties, Mapping):
            properties.update(member_properties)
        member_required = member.get("required")
        if isinstance(member_required, list):
            required.extend(item for item in member_required if isinstance(item, str))
    return properties, required


def _pinned_kind(properties: Mapping[str, Any]) -> str | None:
    """The `kind` this variant pins, if it pins exactly one."""
    kind_property = properties.get("kind", {})
    if not isinstance(kind_property, Mapping):
        return None
    literal = kind_property.get("const")
    if isinstance(literal, str):
        return literal
    enum = kind_property.get("enum")
    if isinstance(enum, list) and len(enum) == 1 and isinstance(enum[0], str):
        return enum[0]
    return None


def _gates() -> dict[str, Any]:
    """Gate kinds and what each MECHANICALLY checks, plus availability on this host."""
    return {
        "independence_rule": (
            "A gate must be a DIFFERENT object from the worker that produced the result, and its "
            "check must be mechanical. 'A model reviewed it and said it looks fine' is not a "
            "gate — the worker can satisfy it by rewording."
        ),
        "kinds": [
            {
                "kind": gate["kind"],
                "checks": gate.get("description") or gate.get("checks"),
                "available_here": gate.get("available"),
                "requires": gate.get("requires", []),
            }
            for gate in list_gates()
        ],
        "graph_nodes": (
            "A loop node is verified by its own package's gate. Other kinds are verified by their "
            "kind's mechanical check (a join's mode, a router's route coverage, an approval's "
            "recorded grant)."
        ),
    }


def _isolation(platform: PlatformSnapshot) -> dict[str, Any]:
    """Each tier, what it enforces here, and which tiers no host can ever deliver."""
    return {
        "tiers": [
            {
                "level": fact.level,
                "deliverable_here": fact.deliverable_here,
                "reason_if_not": fact.reason_if_not,
                "controls_enforced_here": list(fact.controls_enforced_here),
            }
            for fact in platform.isolation
        ],
        "never_available": sorted(
            fact.level for fact in platform.isolation if not fact.available_anywhere
        ),
        "fails_closed": (
            "A node whose tier cannot be delivered is REFUSED before the run starts. The engine "
            "never downgrades isolation silently."
        ),
    }


def _failure_policies(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Declared vs honoured, read from the schema annotation and the validator constant."""
    annotation = schema["$defs"]["baseNode"]["properties"]["on_failure"]
    return {
        "declared": sorted(_ON_FAILURE_DECLARED),
        "honoured": sorted(_ON_FAILURE_DECLARED - _ON_FAILURE_UNIMPLEMENTED),
        "refused": sorted(_ON_FAILURE_UNIMPLEMENTED),
        "schema_annotation": sorted(annotation.get("x-unimplemented", ())),
        "why_refused": (
            "The runtime routes every failure to fail_graph. Accepting a policy it does not route "
            "would hand back a plan whose declared failure policy is silently discarded, so the "
            "compiler refuses it instead."
        ),
    }


def _effects() -> dict[str, Any]:
    """The effect vocabulary, and which effects change what the engine will allow."""
    return {
        "vocabulary": [effect.value for effect in Effect],
        "network_bearing": sorted(effect.value for effect in NETWORK_EFFECTS),
        "retry_requires_idempotency_key": sorted(effect.value for effect in NETWORK_EFFECTS),
        "rule": (
            "A node carrying an external, financial, or irreversible effect cannot retry without "
            "a per-effect idempotency key — retrying an irreversible effect is a double-spend. "
            "Either supply the key or set max_attempts to 1."
        ),
        "approval": (
            "An approval node authorizes the effects it DECLARES. An approval declaring no "
            "effects authorizes nothing."
        ),
    }


def _budgets() -> list[dict[str, Any]]:
    """Every authorable budget field, its unit, and where it is actually enforced."""
    return [
        {
            "field": "max_attempts",
            "unit": "attempts",
            "ceiling": _MAX_ATTEMPTS_CEILING,
            "enforced_by": "the controller's retry loop, per node, per repair round",
        },
        {
            "field": "max_wallclock_s",
            "unit": "seconds",
            "enforced_by": (
                "compiled to hard_deadline_ms and applied as the worker subprocess deadline "
                "(sandboxed and local-CLI workers both)"
            ),
            "note": "Per ATTEMPT, so a node's total wall time is up to max_attempts x this.",
        },
        {
            "field": "max_tokens",
            "unit": "tokens",
            "enforced_by": "node spend accounting; the run pauses when the ceiling is reached",
        },
        {
            "field": "max_cost_microunits",
            "unit": "microunits (1e-6 of the price table's currency)",
            "enforced_by": "node spend accounting; the run pauses when the ceiling is reached",
        },
    ]
