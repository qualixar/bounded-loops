"""Immutable, portable authoring-graph values.

This module intentionally knows nothing about providers, credentials, files,
or runtime state. The compiler is the only later layer allowed to resolve an
authoring slot into an admitted connection.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from types import MappingProxyType
from typing import Mapping


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(child) for key, child in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(child) for child in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(child) for child in value)
    return value


class DataClass(str, Enum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class Effect(str, Enum):
    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    EXTERNAL_WRITE = "external_write"
    FINANCIAL = "financial"
    IRREVERSIBLE = "irreversible"


# Canonical network-effect floor: the effects whose presence forces a node off
# NetworkMode.DENY (the node must be allowed to egress). Single source of truth for every
# network-posture decision across layers — the mid-run envelope check
# (application/execution_policy.py), the pre-run host-capability gate
# (adapters/enforcement/enforcer.py), and the local_cli egress preflight
# (application/egress_posture_policy.py) all import THIS set. Adding a new Effect above?
# Decide here whether it is network-bearing — never re-hardcode this frozenset elsewhere.
NETWORK_EFFECTS = frozenset({Effect.EXTERNAL_WRITE, Effect.FINANCIAL, Effect.IRREVERSIBLE})

# Null/unknown policy digest sentinel — used by the compiler and execution layer when
# no real policy digest is available (e.g., a demo run with no deployed policy).
# Defined here rather than inline so every caller imports the same value and a
# search for ``_NULL_POLICY_DIGEST`` gives a single authoritative definition (ARCH-06).
_NULL_POLICY_DIGEST = "sha256:" + "a" * 64


class IsolationLevel(str, Enum):
    WORKSPACE_ONLY = "workspace_only"
    PROCESS_RESTRICTED = "process_restricted"
    CONTAINER_RESTRICTED = "container_restricted"
    CUSTOMER_MANAGED_WORKER = "customer_managed_worker"


class NodeKind(str, Enum):
    LOOP = "loop"
    TOOL = "tool"
    ROUTER = "router"
    JOIN = "join"
    APPROVAL = "approval"
    AUDIT = "audit"
    RESEARCH_SOURCE = "research_source"
    RESEARCH_CLAIM = "research_claim"
    SUBGRAPH = "subgraph"
    PUBLISH = "publish"


@dataclass(frozen=True)
class GraphBudget:
    max_attempts: int
    max_wallclock_s: int
    max_tokens: int | None = None
    max_cost_microunits: int | None = None


@dataclass(frozen=True)
class PortableBindingSlot:
    id: str
    requires: frozenset[str]
    data_class_max: DataClass
    preferred_modalities: tuple[str, ...]


@dataclass(frozen=True)
class AuthoringNode:
    id: str
    kind: NodeKind
    inputs: Mapping[str, str]
    outputs: Mapping[str, str]
    budget: GraphBudget
    effects: frozenset[Effect]
    isolation: IsolationLevel
    connection_slot: str | None
    on_failure: str | None
    details: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "inputs", _freeze(self.inputs))
        object.__setattr__(self, "outputs", _freeze(self.outputs))
        object.__setattr__(self, "details", _freeze(self.details))


@dataclass(frozen=True)
class AuthoringEdge:
    from_node: str
    from_port: str
    to_node: str
    to_port: str
    when: str | None


@dataclass(frozen=True)
class GraphPolicyIntent:
    data_class: DataClass
    fail_mode: str
    required_audit_profile: str | None


@dataclass(frozen=True)
class AuthoringGraphSpec:
    api_version: str
    graph_id: str
    version: str
    nodes: tuple[AuthoringNode, ...]
    edges: tuple[AuthoringEdge, ...]
    connection_slots: tuple[PortableBindingSlot, ...]
    policies: GraphPolicyIntent
    presentation: Mapping[str, object]
    canonical_json: bytes
    digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "presentation", _freeze(self.presentation))


def canonical_json(value: object) -> bytes:
    """Produce the cross-platform bytes used by graph and plan digests."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value)).hexdigest()
