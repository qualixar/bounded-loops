"""Typed, immutable graph-controller event values."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(child) for key, child in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(child) for child in value)
    return value


@dataclass(frozen=True)
class GraphRunIdentity:
    organization_id: str
    project_id: str
    run_id: str
    graph_digest: str
    plan_digest: str
    policy_digest: str


@dataclass(frozen=True)
class UnsignedGraphEvent:
    event_id: str
    idempotency_key: str
    event_type: str
    timestamp: str
    actor: str
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _freeze(self.payload))


@dataclass(frozen=True)
class StoredGraphEvent:
    identity: GraphRunIdentity
    sequence: int
    event: UnsignedGraphEvent
    previous_hash: str
    event_hash: str


@dataclass(frozen=True)
class VerifiedGraphEventSnapshot:
    """One coherent verified read of a graph receipt stream."""

    receipts: tuple[StoredGraphEvent, ...]
    projection: GraphRunProjection


@dataclass(frozen=True)
class GraphRunProjection:
    state: str
    sequence: int
    head_hash: str
