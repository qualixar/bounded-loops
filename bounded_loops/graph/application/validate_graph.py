"""Strict parsing and validation for portable authoring graphs."""

from __future__ import annotations

import json
from collections.abc import Mapping
import re
from typing import Any

import yaml

from bounded_loops.graph.domain.authoring import (
    AuthoringEdge,
    AuthoringGraphSpec,
    AuthoringNode,
    DataClass,
    Effect,
    GraphBudget,
    GraphPolicyIntent,
    IsolationLevel,
    NETWORK_EFFECTS,
    NodeKind,
    PortableBindingSlot,
    canonical_json,
    digest,
)
from bounded_loops.graph.domain.errors import GraphValidationError


_API_VERSION = "bounded-loops.dev/graph/v1"
_ID = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
_GRAPH_ID = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_ABSOLUTE = re.compile(r"^(?:/|\\\\|[A-Za-z]:[\\/]|~[\\/])")
_SECRET_WORDS = frozenset({"api_key", "credential", "password", "secret", "token"})
# The only budget field names that collide with a secret word. Exempted from the
# secret-shape check when they hold an integer — see _is_declared_quantity.
_BUDGET_QUANTITY_KEYS = frozenset({"max_tokens", "max_cost_microunits"})
# Provider names that must NOT appear in a connection slot's ``requires``. A slot declares
# CAPABILITIES so the graph stays portable across deployments; naming a provider there pins
# the graph to one vendor. This is a DENYLIST for portability, not an allowlist of usable
# providers — a provider is bound at deployment time, not in the authored graph.
# Must cover every provider the project ships, or a slot naming the uncovered one silently
# defeats the rule; a tripwire test asserts it stays a superset of the shipped CLI profiles.
_PROVIDERS = frozenset({
    "agy", "anthropic", "claude", "codex", "grok", "kimi", "muse", "openai", "openrouter",
    "qwen",
})
_ON_FAILURE_DECLARED = frozenset({"fail_graph", "continue", "repair", "await_human"})
# Declared in the authoring schema but NOT routed by GraphRunController: every failure
# currently becomes fail_graph.  Accepting these would silently discard the author's
# declared policy, so validation refuses them until the runtime honours them.
_ON_FAILURE_UNIMPLEMENTED = frozenset({"continue", "repair", "await_human"})
# Mirrors ``run_graph._MAX_ATTEMPTS_CEILING`` so an over-large budget is refused when the
# graph is authored rather than when it runs.  Narrowed from 1000: the retry budget
# multiplies the gate's per-attempt false-accept probability, so a very large budget
# quietly erodes the guarantee the independent gate exists to provide.
_MAX_ATTEMPTS_CEILING = 100
_BASE_NODE_FIELDS = frozenset({
    "id", "kind", "inputs", "outputs", "budget", "effects", "isolation", "connection_slot", "on_failure",
})
_KIND_FIELDS: dict[NodeKind, frozenset[str]] = {
    NodeKind.LOOP: frozenset({"loop_package"}),
    NodeKind.TOOL: frozenset({"tool_ref"}),
    NodeKind.ROUTER: frozenset({"routes", "default_route"}),
    NodeKind.JOIN: frozenset({"mode"}),
    NodeKind.APPROVAL: frozenset({"required_role"}),
    NodeKind.AUDIT: frozenset({"audit_profile"}),
    NodeKind.RESEARCH_SOURCE: frozenset({"source_policy"}),
    NodeKind.RESEARCH_CLAIM: frozenset(),
    NodeKind.SUBGRAPH: frozenset({"graph_package"}),
    NodeKind.PUBLISH: frozenset({"publication_policy"}),
}


class _DuplicateKey(ValueError):
    pass


def _error(code: str, pointer: str, message: str) -> GraphValidationError:
    return GraphValidationError(code, pointer, message)


def parse_authoring_graph_json(source: str) -> AuthoringGraphSpec:
    try:
        raw = json.loads(source, object_pairs_hook=_json_object)
    except _DuplicateKey as exc:
        raise _error("duplicate_key", "/", str(exc)) from exc
    except json.JSONDecodeError as exc:
        raise _error("invalid_json", "/", exc.msg) from exc
    return validate_authoring_graph(raw)


def parse_authoring_graph_yaml(source: str) -> AuthoringGraphSpec:
    try:
        raw = yaml.load(source, Loader=_UniqueYamlLoader)
    except yaml.YAMLError as exc:
        raise _error("invalid_yaml", "/", str(exc)) from exc
    return validate_authoring_graph(raw)


def validate_authoring_graph(raw: object) -> AuthoringGraphSpec:
    _reject_nonportable(raw, "/")
    graph = _mapping(raw, "/")
    _closed(graph, {"api_version", "graph_id", "version", "nodes", "edges", "connection_slots", "policies", "presentation"}, "/")
    _required(graph, {"api_version", "graph_id", "version", "nodes", "edges", "connection_slots", "policies"}, "/")
    if graph["api_version"] != _API_VERSION:
        raise _error("api_version", "/api_version", f"must be {_API_VERSION}")
    graph_id = _identifier(graph["graph_id"], "/graph_id", _GRAPH_ID)
    version = _string(graph["version"], "/version")
    if not _VERSION.fullmatch(version):
        raise _error("version", "/version", "must be a pinned semantic version")
    nodes = _nodes(graph["nodes"])
    edges = _edges(graph["edges"], nodes)
    slots = _slots(graph["connection_slots"])
    policies = _policies(graph["policies"])
    presentation = _mapping(graph.get("presentation", {}), "/presentation")
    _validate_references(nodes, edges, slots)
    canonical = _canonical_graph(graph_id, version, nodes, edges, slots, policies, presentation)
    return AuthoringGraphSpec(
        api_version=_API_VERSION,
        graph_id=graph_id,
        version=version,
        nodes=nodes,
        edges=edges,
        connection_slots=slots,
        policies=policies,
        presentation=presentation,
        canonical_json=canonical_json(canonical),
        digest=digest(canonical),
    )


def _json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(f"duplicate key {key!r}")
        result[key] = value
    return result


class _UniqueYamlLoader(yaml.SafeLoader):
    pass


def _yaml_mapping(loader: _UniqueYamlLoader, node: yaml.MappingNode, deep: bool = False) -> dict[str, object]:
    result: dict[str, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise yaml.YAMLError("graph mapping keys must be strings")
        if key in result:
            raise yaml.YAMLError(f"duplicate key {key!r}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueYamlLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _yaml_mapping)


def _nodes(raw: object) -> tuple[AuthoringNode, ...]:
    values = _list(raw, "/nodes", minimum=1)
    nodes = tuple(_node(value, index) for index, value in enumerate(values))
    ids = [node.id for node in nodes]
    if len(ids) != len(set(ids)):
        raise _error("duplicate_node_id", "/nodes", "node IDs must be unique")
    return nodes


def _node(raw: object, index: int) -> AuthoringNode:
    pointer = f"/nodes/{index}"
    node = _mapping(raw, pointer)
    kind_text = _string(node.get("kind"), f"{pointer}/kind")
    try:
        kind = NodeKind(kind_text)
    except ValueError as exc:
        raise _error("unknown_node_kind", f"{pointer}/kind", "node kind is not supported") from exc
    allowed = _BASE_NODE_FIELDS | _KIND_FIELDS[kind]
    _closed(node, allowed, pointer)
    _required(node, _BASE_NODE_FIELDS - {"connection_slot", "on_failure"} | _KIND_FIELDS[kind], pointer)
    inputs = _ports(node["inputs"], f"{pointer}/inputs")
    outputs = _ports(node["outputs"], f"{pointer}/outputs")
    budget = _budget(node["budget"], f"{pointer}/budget")
    effects = _effects(node["effects"], f"{pointer}/effects")
    if budget.max_attempts > 1 and (effects & NETWORK_EFFECTS):
        # The controller refuses this combination when it is constructed (D7: an external or
        # irreversible effect cannot be re-driven without a per-effect idempotency key).
        # Catching it here means `bl graph lint` and the studio reject it at authoring time
        # instead of producing a plan that cannot start.
        raise _error(
            "retry_of_effectful_node", f"{pointer}/budget/max_attempts",
            "a node carrying an external / irreversible effect cannot retry without a "
            "per-effect idempotency key (D7); declare max_attempts: 1",
        )
    isolation = _enum(IsolationLevel, node["isolation"], f"{pointer}/isolation", "isolation")
    connection_slot = _optional_identifier(node.get("connection_slot"), f"{pointer}/connection_slot")
    on_failure = node.get("on_failure")
    if on_failure is not None and on_failure not in _ON_FAILURE_DECLARED:
        raise _error("on_failure", f"{pointer}/on_failure", "must be a declared failure policy")
    if on_failure in _ON_FAILURE_UNIMPLEMENTED:
        # Refuse rather than accept-and-ignore.  The runtime routes every failure to
        # fail_graph, so accepting these three would hand back a plan whose declared
        # failure policy is silently discarded — exactly the silent no-op this project
        # forbids of its connectors.  See LLD 01 §3.5 for why each is still deferred.
        raise _error(
            "on_failure_unimplemented", f"{pointer}/on_failure",
            f"on_failure={on_failure!r} is declared but not yet routed by the runtime; "
            "only 'fail_graph' (the default) is honoured today",
        )
    details = {field: node[field] for field in _KIND_FIELDS[kind]}
    _validate_kind_details(kind, details, pointer)
    return AuthoringNode(
        id=_identifier(node["id"], f"{pointer}/id", _ID),
        kind=kind,
        inputs=inputs,
        outputs=outputs,
        budget=budget,
        effects=effects,
        isolation=isolation,
        connection_slot=connection_slot,
        on_failure=on_failure if isinstance(on_failure, str) else None,
        details=details,
    )


def _validate_kind_details(kind: NodeKind, details: Mapping[str, object], pointer: str) -> None:
    digest_field = "loop_package" if kind is NodeKind.LOOP else "graph_package" if kind is NodeKind.SUBGRAPH else None
    if digest_field is not None:
        value = _string(details[digest_field], f"{pointer}/{digest_field}")
        if not _DIGEST.fullmatch(value):
            raise _error("mutable_package_reference", f"{pointer}/{digest_field}", "must be a sha256 digest")
    if kind is NodeKind.TOOL:
        _string(details["tool_ref"], f"{pointer}/tool_ref")
    if kind is NodeKind.ROUTER:
        routes = _mapping(details["routes"], f"{pointer}/routes")
        if not routes:
            raise _error("incomplete_branches", f"{pointer}/routes", "router must declare routes")
        default = details.get("default_route")
        if default is not None and not isinstance(default, str):
            raise _error("incomplete_branches", f"{pointer}/default_route", "must be a string or null")
        if default is None and "default" not in routes:
            raise _error("incomplete_branches", f"{pointer}/routes", "router requires an explicit default")
    if kind is NodeKind.JOIN and details["mode"] not in {"all_selected", "all_successful", "any_successful"}:
        raise _error("join_mode", f"{pointer}/mode", "join mode is not supported")
    for field in {"required_role", "audit_profile", "source_policy", "publication_policy"} & set(details):
        _string(details[field], f"{pointer}/{field}")


def _edges(raw: object, nodes: tuple[AuthoringNode, ...]) -> tuple[AuthoringEdge, ...]:
    values = _list(raw, "/edges")
    known = {node.id: node for node in nodes}
    edges: list[AuthoringEdge] = []
    for index, value in enumerate(values):
        pointer = f"/edges/{index}"
        edge = _mapping(value, pointer)
        _closed(edge, {"from_node", "from_port", "to_node", "to_port", "when"}, pointer)
        _required(edge, {"from_node", "from_port", "to_node", "to_port"}, pointer)
        from_node = _identifier(edge["from_node"], f"{pointer}/from_node", _ID)
        to_node = _identifier(edge["to_node"], f"{pointer}/to_node", _ID)
        from_port = _string(edge["from_port"], f"{pointer}/from_port")
        to_port = _string(edge["to_port"], f"{pointer}/to_port")
        if from_node not in known or to_node not in known:
            raise _error("unknown_edge_node", pointer, "edge references an unknown node")
        if from_port not in known[from_node].outputs:
            raise _error("missing_output_port", f"{pointer}/from_port", "source output does not exist")
        if to_port not in known[to_node].inputs:
            raise _error("missing_input_port", f"{pointer}/to_port", "target input does not exist")
        if known[from_node].outputs[from_port] != known[to_node].inputs[to_port]:
            raise _error("port_type_mismatch", pointer, "edge port types must match")
        when = edge.get("when")
        if when is not None and not isinstance(when, str):
            raise _error("edge_condition", f"{pointer}/when", "must be a string or null")
        edges.append(AuthoringEdge(from_node, from_port, to_node, to_port, when))
    return tuple(edges)


def _slots(raw: object) -> tuple[PortableBindingSlot, ...]:
    values = _list(raw, "/connection_slots")
    slots: list[PortableBindingSlot] = []
    for index, value in enumerate(values):
        pointer = f"/connection_slots/{index}"
        slot = _mapping(value, pointer)
        _closed(slot, {"id", "requires", "data_class_max", "preferred_modalities"}, pointer)
        _required(slot, {"id", "requires", "data_class_max"}, pointer)
        requires = _string_set(slot["requires"], f"{pointer}/requires", minimum=1)
        if _PROVIDERS & {item.lower() for item in requires}:
            raise _error("provider_in_slot", f"{pointer}/requires", "slots declare capabilities, never providers")
        modalities = tuple(_string_list(slot.get("preferred_modalities", []), f"{pointer}/preferred_modalities"))
        slots.append(PortableBindingSlot(
            id=_identifier(slot["id"], f"{pointer}/id", _ID),
            requires=requires,
            data_class_max=_enum(DataClass, slot["data_class_max"], f"{pointer}/data_class_max", "data class"),
            preferred_modalities=modalities,
        ))
    ids = [slot.id for slot in slots]
    if len(ids) != len(set(ids)):
        raise _error("duplicate_slot_id", "/connection_slots", "slot IDs must be unique")
    return tuple(slots)


def _policies(raw: object) -> GraphPolicyIntent:
    policies = _mapping(raw, "/policies")
    _closed(policies, {"data_class", "fail_mode", "required_audit_profile"}, "/policies")
    _required(policies, {"data_class", "fail_mode"}, "/policies")
    fail_mode = policies["fail_mode"]
    if fail_mode not in {"fail_closed", "continue_declared"}:
        raise _error("fail_mode", "/policies/fail_mode", "must be a declared graph failure mode")
    profile = policies.get("required_audit_profile")
    if profile is not None and not isinstance(profile, str):
        raise _error("audit_profile", "/policies/required_audit_profile", "must be a string or null")
    return GraphPolicyIntent(
        data_class=_enum(DataClass, policies["data_class"], "/policies/data_class", "data class"),
        fail_mode=fail_mode,
        required_audit_profile=profile,
    )


def _validate_references(nodes: tuple[AuthoringNode, ...], edges: tuple[AuthoringEdge, ...], slots: tuple[PortableBindingSlot, ...]) -> None:
    slot_ids = {slot.id for slot in slots}
    for node in nodes:
        if node.connection_slot is not None and node.connection_slot not in slot_ids:
            raise _error("unknown_connection_slot", f"/nodes/{node.id}/connection_slot", "slot does not exist")
    incoming = {node.id: 0 for node in nodes}
    adjacent: dict[str, set[str]] = {node.id: set() for node in nodes}
    for edge in edges:
        incoming[edge.to_node] += 1
        adjacent[edge.from_node].add(edge.to_node)
    for node in nodes:
        if node.kind is NodeKind.JOIN and incoming[node.id] == 0:
            raise _error("impossible_join", f"/nodes/{node.id}", "join requires an incoming edge")
    ready = sorted(node_id for node_id, count in incoming.items() if count == 0)
    visited = 0
    while ready:
        current = ready.pop(0)
        visited += 1
        for child in sorted(adjacent[current]):
            incoming[child] -= 1
            if incoming[child] == 0:
                ready.append(child)
    if visited != len(nodes):
        raise _error("cycle", "/edges", "authoring graph must be acyclic")


def _canonical_graph(graph_id: str, version: str, nodes: tuple[AuthoringNode, ...], edges: tuple[AuthoringEdge, ...], slots: tuple[PortableBindingSlot, ...], policies: GraphPolicyIntent, presentation: Mapping[str, object]) -> dict[str, object]:
    return {
        "api_version": _API_VERSION,
        "connection_slots": [{"data_class_max": slot.data_class_max.value, "id": slot.id, "preferred_modalities": list(slot.preferred_modalities), "requires": sorted(slot.requires)} for slot in slots],
        "edges": [{"from_node": edge.from_node, "from_port": edge.from_port, "to_node": edge.to_node, "to_port": edge.to_port, "when": edge.when} for edge in edges],
        "graph_id": graph_id,
        "nodes": [_canonical_node(node) for node in nodes],
        "policies": {"data_class": policies.data_class.value, "fail_mode": policies.fail_mode, "required_audit_profile": policies.required_audit_profile},
        "presentation": dict(presentation),
        "version": version,
    }


def _canonical_node(node: AuthoringNode) -> dict[str, object]:
    return {
        "budget": {"max_attempts": node.budget.max_attempts, "max_cost_microunits": node.budget.max_cost_microunits, "max_tokens": node.budget.max_tokens, "max_wallclock_s": node.budget.max_wallclock_s},
        "connection_slot": node.connection_slot,
        "details": dict(node.details),
        "effects": sorted(effect.value for effect in node.effects),
        "id": node.id,
        "inputs": dict(node.inputs),
        "isolation": node.isolation.value,
        "kind": node.kind.value,
        "on_failure": node.on_failure,
        "outputs": dict(node.outputs),
    }


def _budget(raw: object, pointer: str) -> GraphBudget:
    budget = _mapping(raw, pointer)
    _closed(budget, {"max_attempts", "max_wallclock_s", "max_tokens", "max_cost_microunits"}, pointer)
    _required(budget, {"max_attempts", "max_wallclock_s"}, pointer)
    return GraphBudget(
        max_attempts=_bounded_int(
            budget["max_attempts"], f"{pointer}/max_attempts", 1, _MAX_ATTEMPTS_CEILING,
        ),
        max_wallclock_s=_bounded_int(budget["max_wallclock_s"], f"{pointer}/max_wallclock_s", 1, 86400),
        max_tokens=_optional_int(budget.get("max_tokens"), f"{pointer}/max_tokens", minimum=1),
        max_cost_microunits=_optional_int(budget.get("max_cost_microunits"), f"{pointer}/max_cost_microunits", minimum=0),
    )


def _ports(raw: object, pointer: str) -> dict[str, str]:
    values = _mapping(raw, pointer)
    result: dict[str, str] = {}
    for key, value in values.items():
        result[_identifier(key, f"{pointer}/{key}", _ID)] = _string(value, f"{pointer}/{key}")
    return result


def _effects(raw: object, pointer: str) -> frozenset[Effect]:
    values = _string_list(raw, pointer)
    effects = frozenset(_enum(Effect, value, pointer, "effect") for value in values)
    if len(effects) != len(values):
        raise _error("duplicate_effect", pointer, "effects must be unique")
    return effects


def _reject_nonportable(value: object, pointer: str) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            lowered = str(key).lower()
            if any(word in lowered for word in _SECRET_WORDS) and not _is_declared_quantity(lowered, child):
                raise _error("secret_field", pointer, "authoring graphs cannot contain secret-shaped fields")
            _reject_nonportable(child, f"{pointer}/{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_nonportable(child, f"{pointer}/{index}")
    elif isinstance(value, str) and _ABSOLUTE.match(value):
        raise _error("absolute_path", pointer, "authoring graphs cannot contain absolute local paths")


def _is_declared_quantity(key: str, value: object) -> bool:
    """Whether this key is a NAMED budget quantity holding an integer.

    ``_SECRET_WORDS`` is matched as a SUBSTRING, which is right for catching ``auth_token`` and
    ``github_api_key`` but wrong for ``max_tokens`` — a false positive that made the field
    unauthorable in every manifest while it sat in the JSON schema, the closed allowed-set,
    ``GraphBudget`` and the compiled plan.

    The exemption is an explicit ALLOWLIST of the two field names that needed it, not "any
    number under any secret-shaped name". The broader rule was tried first and let
    ``api_key: 999999`` through: numeric identifiers, PINs and account numbers are real, so
    "no integer is a credential" is an assumption this check has no business making. Scoping to
    named fields gives up nothing — those two are the only budget keys whose names collide with
    a secret word — and it keeps the check refusing everything it refused before.

    The integer requirement is belt and braces: ``max_tokens: "sk-live-…"`` is still refused.
    ``bool`` is excluded because it is an int subclass, and a flag is not a quantity.
    """
    if key not in _BUDGET_QUANTITY_KEYS:
        return False
    return isinstance(value, int) and not isinstance(value, bool)


def _mapping(value: object, pointer: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise _error("type", pointer, "must be an object with string keys")
    return value


def _list(value: object, pointer: str, *, minimum: int = 0) -> list[object]:
    if not isinstance(value, list) or len(value) < minimum:
        raise _error("type", pointer, f"must be an array with at least {minimum} item(s)")
    return value


def _string(value: object, pointer: str) -> str:
    if not isinstance(value, str) or not value:
        raise _error("type", pointer, "must be a non-empty string")
    return value


def _identifier(value: object, pointer: str, pattern: re.Pattern[str]) -> str:
    text = _string(value, pointer)
    if not pattern.fullmatch(text):
        raise _error("identifier", pointer, "is not a stable portable identifier")
    return text


def _optional_identifier(value: object, pointer: str) -> str | None:
    return None if value is None else _identifier(value, pointer, _ID)


def _string_list(value: object, pointer: str) -> list[str]:
    return [_string(item, f"{pointer}/{index}") for index, item in enumerate(_list(value, pointer))]


def _string_set(value: object, pointer: str, *, minimum: int = 0) -> frozenset[str]:
    values = _string_list(value, pointer)
    if len(values) < minimum or len(values) != len(set(values)):
        raise _error("duplicate_value", pointer, "must contain unique declared values")
    return frozenset(values)


def _bounded_int(value: object, pointer: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise _error("range", pointer, f"must be an integer from {minimum} to {maximum}")
    return value


def _optional_int(value: object, pointer: str, *, minimum: int) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise _error("range", pointer, f"must be null or an integer >= {minimum}")
    return value


def _enum(enum: type[Any], value: object, pointer: str, label: str) -> Any:
    try:
        return enum(value)
    except ValueError as exc:
        raise _error("enum", pointer, f"must be a supported {label}") from exc


def _closed(value: Mapping[str, object], allowed: set[str] | frozenset[str], pointer: str) -> None:
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise _error("unknown_field", pointer, f"unknown field(s): {', '.join(unknown)}")


def _required(value: Mapping[str, object], required: set[str] | frozenset[str], pointer: str) -> None:
    missing = sorted(set(required) - set(value))
    if missing:
        raise _error("missing_field", pointer, f"missing field(s): {', '.join(missing)}")
