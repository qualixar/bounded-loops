"""Agent memory spine — a tenant-scoped, namespaced working-memory store (ADR-12 D3).

This is NOT the run's durable truth (that is the append-only event log). Per ADR-12
D4, agent memory and STATE.md are UX / working state, never authority: a node may read
and write namespaced memory that survives across runs, but a memory value can never
substitute for a receipt. The store is tenant-scoped BY CONSTRUCTION — one store serves
exactly one (organization, project), so a namespace in one tenant can never read or
overwrite another tenant's memory. Values must round-trip through JSON and stay under a
byte cap, so a node cannot smuggle non-serializable or unbounded state into the spine.
Reads return an independent copy, so a caller cannot mutate stored memory in place.

``GraphMemoryStorePort`` is the contract; ``InMemoryGraphMemoryStore`` is the reference
used in tests and single-process runs. A durable SLM-backed adapter satisfies the same
port and is a deployment binding (it scopes SLM keys to the tenant).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Callable, Protocol

from bounded_loops.graph.domain.errors import GraphValidationError

_DEFAULT_MAX_VALUE_BYTES = 256 * 1024
_MAX_IDENTIFIER_BYTES = 1024  # a namespace part or key is an identifier, not a place to smuggle bulk state

MemoryNamespace = tuple[str, ...]


@dataclass(frozen=True)
class MemoryRecord:
    """One stored memory item within a tenant. ``value`` is a JSON round-trippable
    copy, decoupled from whatever the caller stored or later mutates."""

    namespace: MemoryNamespace
    key: str
    value: object
    updated_at: str  # ISO-8601 UTC


class GraphMemoryStorePort(Protocol):
    """Tenant-scoped namespaced memory. Every operation stays within the store's own
    (organization, project); the port exposes no cross-tenant access."""

    def put(self, namespace: MemoryNamespace, key: str, value: object) -> MemoryRecord: ...
    def get(self, namespace: MemoryNamespace, key: str) -> MemoryRecord | None: ...
    def search(self, namespace: MemoryNamespace) -> tuple[MemoryRecord, ...]: ...
    def delete(self, namespace: MemoryNamespace, key: str) -> bool: ...


class InMemoryGraphMemoryStore:
    """A reference, single-process, tenant-bound memory store.

    Isolation is structural: the instance holds one tenant's data, so no API can reach
    another tenant's memory. The store keeps the SERIALIZED form and decodes a fresh
    object on every read, so stored memory is immutable to callers. Namespaces and keys
    are validated non-empty, and values must be JSON-serializable and under the cap.
    """

    def __init__(
        self,
        *,
        organization_id: str,
        project_id: str,
        max_value_bytes: int = _DEFAULT_MAX_VALUE_BYTES,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if (
            not isinstance(organization_id, str) or not organization_id.strip()
            or not isinstance(project_id, str) or not project_id.strip()
        ):
            raise GraphValidationError("memory_tenant", "/", "organization and project are required")
        self._organization_id = organization_id
        self._project_id = project_id
        self._max_value_bytes = max_value_bytes
        self._clock = clock if clock is not None else (lambda: datetime.now(timezone.utc))
        self._records: dict[tuple[MemoryNamespace, str], tuple[str, str]] = {}

    @property
    def tenant(self) -> tuple[str, str]:
        return (self._organization_id, self._project_id)

    def put(self, namespace: MemoryNamespace, key: str, value: object) -> MemoryRecord:
        ns = _validate_namespace(namespace)
        _validate_key(key)
        serialized = _serialize(value)
        if len(serialized.encode("utf-8")) > self._max_value_bytes:
            raise GraphValidationError("memory_value", "/value", "value exceeds the memory byte cap")
        updated_at = _iso(self._clock())
        self._records[(ns, key)] = (serialized, updated_at)
        return MemoryRecord(ns, key, json.loads(serialized), updated_at)

    def get(self, namespace: MemoryNamespace, key: str) -> MemoryRecord | None:
        ns = _validate_namespace(namespace)
        _validate_key(key)
        entry = self._records.get((ns, key))
        if entry is None:
            return None
        serialized, updated_at = entry
        return MemoryRecord(ns, key, json.loads(serialized), updated_at)

    def search(self, namespace: MemoryNamespace) -> tuple[MemoryRecord, ...]:
        ns = _validate_namespace(namespace)
        records = [
            MemoryRecord(record_ns, key, json.loads(serialized), updated_at)
            for (record_ns, key), (serialized, updated_at) in self._records.items()
            if record_ns == ns
        ]
        return tuple(sorted(records, key=lambda record: record.key))

    def delete(self, namespace: MemoryNamespace, key: str) -> bool:
        ns = _validate_namespace(namespace)
        _validate_key(key)
        return self._records.pop((ns, key), None) is not None


def _validate_namespace(namespace: MemoryNamespace) -> MemoryNamespace:
    if not isinstance(namespace, tuple) or not namespace:
        raise GraphValidationError("memory_namespace", "/namespace", "namespace must be a non-empty tuple")
    for part in namespace:
        if not isinstance(part, str) or not part.strip():
            raise GraphValidationError("memory_namespace", "/namespace", "namespace parts must be non-empty strings")
        if len(part.encode("utf-8")) > _MAX_IDENTIFIER_BYTES:
            raise GraphValidationError("memory_namespace", "/namespace", "namespace part exceeds the identifier byte cap")
    return namespace


def _validate_key(key: str) -> None:
    if not isinstance(key, str) or not key.strip():
        raise GraphValidationError("memory_key", "/key", "key must be a non-empty string")
    if len(key.encode("utf-8")) > _MAX_IDENTIFIER_BYTES:
        raise GraphValidationError("memory_key", "/key", "key exceeds the identifier byte cap")


def _serialize(value: object) -> str:
    try:
        # allow_nan=False: NaN/Infinity are not valid JSON (RFC 8259); a store that
        # promises a JSON round-trip must refuse them rather than persist a token no
        # strict parser can read back.
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise GraphValidationError("memory_value", "/value", "value must be JSON-serializable") from exc


def _iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()
