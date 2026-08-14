"""Form descriptors derived from `authoring-graph.schema.json`, not hand-written.

This is the single most important decision in the UI. The Studio page that shipped in 0.4.0 has
eight hand-written fields and the compiler accepts far more than eight — so the UI silently could
not express most of what the engine does, and every new authoring field needed a second edit
somewhere nobody remembered. A form generated from the schema cannot drift: a field added to the
schema appears in the UI for free, and a field removed disappears.

It also carries the honesty annotations. The schema marks `on_failure: continue` / `await_human`
as `x-unimplemented` and the `customer_managed_worker` isolation tier as `x-never-available`,
because the compiler refuses all three. Those values arrive here as **unavailable choices** — the
UI renders them disabled with the reason, rather than offering a non-technical user a dropdown
option whose every graph is rejected at compile.

Pure application logic: no HTTP, no filesystem beyond the packaged schema, no rendering.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from bounded_loops.graph.application.schemas import authoring_graph_schema

#: Annotation keys the schema uses to mark a value the compiler will not accept. Read here rather
#: than restated, so the UI and the validator cannot disagree about what is offerable.
_UNAVAILABLE_KEYS = ("x-unimplemented", "x-never-available")

_UNAVAILABLE_REASON = {
    "x-unimplemented": "declared by the schema but not routed by the runtime — the compiler refuses it",
    "x-never-available": "cannot be enforced on any host — a graph using it fails closed everywhere",
}


@dataclass(frozen=True)
class Choice:
    """One option in a closed field, and whether it can actually be used."""

    value: str
    available: bool
    reason: str | None = None


@dataclass(frozen=True)
class Field:
    """One editable field, described well enough for a UI to render it without guessing."""

    name: str
    kind: str                      # string | integer | boolean | enum | object | array | any
    required: bool
    description: str | None = None
    choices: tuple[Choice, ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    value_kind: str | None = None  # for object/array: what the entries hold
    pattern: str | None = None     # e.g. the digestRef shape, so a UI can validate before sending
    #: Sub-fields, for a `$ref` to an object like `budget`. Without these a form renders "budget"
    #: as an opaque blob and the four spend ceilings inside it stay unreachable — which is exactly
    #: how the old Studio ended up unable to set max_tokens or max_cost_microunits.
    fields: tuple[Field, ...] = ()

    @property
    def offerable(self) -> tuple[str, ...]:
        """Only the choices a UI should let someone pick."""
        return tuple(choice.value for choice in self.choices if choice.available)


def node_form(kind: str) -> tuple[Field, ...]:
    """Every field authorable on a node of `kind`: the base node's, then the kind's own.

    Raises KeyError for a kind the schema does not define, rather than returning an empty form —
    an empty form looks like "this kind needs nothing", which is the wrong answer to a typo.
    """
    schema = authoring_graph_schema()
    base = schema["$defs"]["baseNode"]
    variant_properties, variant_required = _variant_for(schema, kind)

    fields = _fields_from(base.get("properties", {}), set(base.get("required", ())))
    kind_fields = _fields_from(
        {name: value for name, value in variant_properties.items() if name != "kind"},
        set(variant_required),
    )
    return fields + kind_fields


def graph_form() -> tuple[Field, ...]:
    """The graph-level fields: identity, plus the policy block."""
    schema = authoring_graph_schema()
    top = _fields_from(
        {
            name: value
            for name, value in schema["properties"].items()
            if name not in {"nodes", "edges", "connection_slots"}
        },
        set(schema.get("required", ())),
    )
    policies_def = schema["$defs"]["policies"]
    policies = _fields_from(
        policies_def.get("properties", {}), set(policies_def.get("required", ()))
    )
    return top + policies


def edge_form() -> tuple[Field, ...]:
    schema = authoring_graph_schema()
    edge = schema["$defs"]["edge"]
    return _fields_from(edge.get("properties", {}), set(edge.get("required", ())))


def node_kinds() -> tuple[str, ...]:
    """Every kind the schema pins a variant for, in schema order."""
    schema = authoring_graph_schema()
    kinds = []
    for variant in schema["$defs"]["node"].get("oneOf", ()):
        properties, _required = _flatten(variant)
        pinned = _pinned_kind(properties)
        if pinned is not None:
            kinds.append(pinned)
    return tuple(kinds)


def form_document() -> Mapping[str, Any]:
    """The whole form definition, JSON-ready, for one request from the UI."""
    return {
        "graph": [_field_dict(field) for field in graph_form()],
        "edge": [_field_dict(field) for field in edge_form()],
        "nodes": {
            kind: [_field_dict(field) for field in node_form(kind)] for kind in node_kinds()
        },
        "generated_from": "bounded_loops/graph/schemas/authoring-graph.schema.json",
        "why": (
            "These fields are derived from the compiler's own schema. A field the compiler "
            "accepts appears here automatically; a value it refuses is marked unavailable rather "
            "than offered."
        ),
    }


# ── internals ────────────────────────────────────────────────────────────────


def _variant_for(schema: Mapping[str, Any], kind: str) -> tuple[dict[str, Any], list[str]]:
    for variant in schema["$defs"]["node"].get("oneOf", ()):
        properties, required = _flatten(variant)
        if _pinned_kind(properties) == kind:
            return properties, required
    raise KeyError(f"the authoring schema defines no node kind {kind!r}")


def _flatten(variant: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Merge a oneOf variant's own properties/required with those of its allOf members.

    Each kind is written as `oneOf[i].allOf = [{$ref: baseNode}, {the kind's own bits}]`, so
    reading `variant["properties"]` alone finds nothing. `$ref` members are skipped: the base node
    is added separately by `node_form`, and merging it here would duplicate every base field.
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


def _fields_from(
    properties: Mapping[str, Any],
    required: set[str],
    schema: Mapping[str, Any] | None = None,
) -> tuple[Field, ...]:
    document = schema if schema is not None else authoring_graph_schema()
    return tuple(
        _field(name, definition, name in required, document)
        for name, definition in sorted(properties.items())
        if isinstance(definition, Mapping)
    )


def _field(
    name: str, definition: Mapping[str, Any], required: bool, schema: Mapping[str, Any],
) -> Field:
    resolved = _resolve(_deref(definition, schema))
    nested: tuple[Field, ...] = ()
    if isinstance(resolved.get("properties"), Mapping):
        nested = _fields_from(
            resolved["properties"], set(resolved.get("required", ())), schema,
        )
    pattern = resolved.get("pattern")
    return Field(
        name=name,
        kind=_kind_of(resolved),
        required=required,
        description=resolved.get("description"),
        choices=_choices(resolved),
        minimum=resolved.get("minimum") if isinstance(resolved.get("minimum"), int) else None,
        maximum=resolved.get("maximum") if isinstance(resolved.get("maximum"), int) else None,
        value_kind=_value_kind(resolved),
        pattern=pattern if isinstance(pattern, str) else None,
        fields=nested,
    )


def _deref(definition: Mapping[str, Any], schema: Mapping[str, Any]) -> Mapping[str, Any]:
    """Follow a local `$ref` into `$defs`, keeping any sibling keys the reference carries.

    Without this, `budget` and `loop_package` arrive as `{"$ref": ...}` and render as opaque
    "any" fields — so the four spend ceilings inside `budget` would be unreachable from the UI,
    reproducing the exact gap that made the old Studio unable to set them.

    Only local `#/$defs/...` references are followed. A remote `$ref` is left alone rather than
    fetched: a form generator must not make a network request, and a schema that needs one is a
    packaging problem to fix, not something to paper over here.
    """
    reference = definition.get("$ref")
    if not isinstance(reference, str) or not reference.startswith("#/$defs/"):
        return definition
    target = schema.get("$defs", {}).get(reference.removeprefix("#/$defs/"))
    if not isinstance(target, Mapping):
        return definition
    merged = dict(target)
    merged.update({key: value for key, value in definition.items() if key != "$ref"})
    return merged


def _resolve(definition: Mapping[str, Any]) -> Mapping[str, Any]:
    """Collapse a `oneOf` down to the branch a form can edit, keeping the annotations.

    `on_failure` is `oneOf[{enum}, {repair object}]`. A form offers the enum and treats the object
    form as an advanced case, so the enum branch is the editable one — but the annotations live on
    the OUTER object, which is why they are merged forward rather than read from the branch.
    """
    branches = definition.get("oneOf")
    if not isinstance(branches, list) or not branches:
        return definition
    for branch in branches:
        if isinstance(branch, Mapping) and "enum" in branch:
            merged = dict(branch)
            for key in (*_UNAVAILABLE_KEYS, "description"):
                if key in definition:
                    merged[key] = definition[key]
            return merged
    first = branches[0]
    return first if isinstance(first, Mapping) else definition


def _kind_of(definition: Mapping[str, Any]) -> str:
    if "enum" in definition:
        return "enum"
    declared = definition.get("type")
    if isinstance(declared, list):
        # e.g. ["string", "null"] — editable as the non-null member.
        concrete = [item for item in declared if item != "null"]
        declared = concrete[0] if concrete else "any"
    return {
        "string": "string",
        "integer": "integer",
        "number": "integer",
        "boolean": "boolean",
        "object": "object",
        "array": "array",
    }.get(declared if isinstance(declared, str) else "", "any")


def _choices(definition: Mapping[str, Any]) -> tuple[Choice, ...]:
    values = definition.get("enum")
    if not isinstance(values, list):
        return ()
    unavailable: dict[str, str] = {}
    for key in _UNAVAILABLE_KEYS:
        listed = definition.get(key)
        if isinstance(listed, list):
            for value in listed:
                if isinstance(value, str):
                    unavailable[value] = _UNAVAILABLE_REASON[key]
    return tuple(
        Choice(
            value=str(value),
            available=str(value) not in unavailable,
            reason=unavailable.get(str(value)),
        )
        for value in values
    )


def _value_kind(definition: Mapping[str, Any]) -> str | None:
    """What an object's values or an array's items hold, when the schema says."""
    additional = definition.get("additionalProperties")
    if isinstance(additional, Mapping):
        return _kind_of(additional)
    items = definition.get("items")
    if isinstance(items, Mapping):
        return _kind_of(items)
    return None


def _field_dict(field: Field) -> dict[str, Any]:
    return {
        "name": field.name,
        "kind": field.kind,
        "required": field.required,
        "description": field.description,
        "choices": [
            {"value": choice.value, "available": choice.available, "reason": choice.reason}
            for choice in field.choices
        ],
        "minimum": field.minimum,
        "maximum": field.maximum,
        "value_kind": field.value_kind,
        "pattern": field.pattern,
        "fields": [_field_dict(nested) for nested in field.fields],
    }


def offerable_values(fields: Sequence[Field], name: str) -> tuple[str, ...]:
    """The pickable values of one named field — the helper a form generator actually wants."""
    for field in fields:
        if field.name == name:
            return field.offerable
    raise KeyError(f"no field named {name!r}")
