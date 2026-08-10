"""Durable, SLM-pluggable memory spine — KeyValueBackedMemoryStore over a DurableKeyValuePort.

The store persists through a durable key/value backend (SLM in production; a shared
in-memory fake here). Because a shared backend holds EVERY tenant's data, the security
core is key construction: an injection-safe netstring encoding of (org, project,
namespace, key) so no crafted namespace/key can read, list, or collide with another
tenant's or namespace's entries. Values stay JSON-only, byte-capped, and read-immutable.
"""

from __future__ import annotations

import json

import pytest

from bounded_loops.graph.application.memory_store import KeyValueBackedMemoryStore
from bounded_loops.graph.domain.errors import GraphIntegrityError, GraphValidationError

_NS = ("memories",)


class _FakeKv:
    """A dict-backed durable KV standing in for SLM / any durable backend. SHARED
    across store instances in a test to model one real backend holding all tenants."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def set(self, key: str, value: str) -> None:
        self.store[key] = value

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def delete(self, key: str) -> bool:
        return self.store.pop(key, None) is not None

    def list_prefix(self, prefix: str) -> tuple[tuple[str, str], ...]:
        return tuple((key, value) for key, value in self.store.items() if key.startswith(prefix))


def _store(kv: _FakeKv | None = None, *, org: str = "org-1", project: str = "project-1", **over):
    return KeyValueBackedMemoryStore(
        organization_id=org, project_id=project, kv=kv if kv is not None else _FakeKv(), **over,
    )


# ── behavior parity with the in-memory reference ────────────────────────────────

def test_put_get_roundtrip_and_overwrite():
    store = _store()
    store.put(_NS, "k", {"a": [1, 2]})
    got = store.get(_NS, "k")
    assert got is not None and got.value == {"a": [1, 2]}
    store.put(_NS, "k", "second")
    assert store.get(_NS, "k").value == "second"  # type: ignore[union-attr]


def test_get_miss_returns_none_and_delete_reports_removal():
    store = _store()
    assert store.get(_NS, "absent") is None
    store.put(_NS, "k", 1)
    assert store.delete(_NS, "k") is True
    assert store.get(_NS, "k") is None
    assert store.delete(_NS, "k") is False


def test_a_read_value_mutation_does_not_change_stored_memory():
    store = _store()
    store.put(_NS, "k", {"items": [1]})
    first = store.get(_NS, "k")
    assert first is not None
    first.value["items"].append(999)  # type: ignore[index]
    second = store.get(_NS, "k")
    assert second is not None and second.value == {"items": [1]}


def test_non_serializable_and_oversize_values_are_refused():
    store = _store(max_value_bytes=16)
    with pytest.raises(GraphValidationError, match="JSON-serializable"):
        store.put(_NS, "k", {1, 2})
    with pytest.raises(GraphValidationError, match="byte cap"):
        store.put(_NS, "k", "x" * 100)


# ── the durable / shared-backend security core ─────────────────────────────────

def test_a_shared_backend_still_isolates_tenants():
    kv = _FakeKv()
    a = _store(kv, org="org-a")
    b = _store(kv, org="org-b")
    a.put(_NS, "k", "a-secret")

    # Same namespace + key, different tenant sharing ONE backend — no read, no list.
    assert b.get(_NS, "k") is None
    assert b.search(_NS) == ()
    assert a.get(_NS, "k").value == "a-secret"  # type: ignore[union-attr]


def test_search_is_exact_namespace_not_sub_namespaces():
    store = _store()
    store.put(("a",), "k1", 1)
    store.put(("a", "sub"), "k2", 2)
    assert [record.key for record in store.search(("a",))] == ["k1"]
    assert [record.key for record in store.search(("a", "sub"))] == ["k2"]


def test_no_crafted_tenant_or_namespace_can_reach_anothers_entry():
    kv = _FakeKv()
    victim = _store(kv, org="acme", project="prod")
    victim.put(("billing",), "card", "sensitive")

    # Cross-tenant attackers whose NAIVE concatenation could swallow the victim's
    # segments — netstring length-prefixing makes every boundary unambiguous.
    for attacker in (
        _store(kv, org="acme", project="prod:billing"),
        _store(kv, org="acmeprod", project="billing"),
    ):
        assert attacker.get(("billing",), "card") is None
        assert attacker.search(("billing",)) == ()

    # Same tenant, WRONG namespace still cannot see it.
    assert victim.get(("payments",), "card") is None
    # Only the exact (tenant, namespace, key) reads it.
    assert victim.get(("billing",), "card").value == "sensitive"  # type: ignore[union-attr]


def test_memory_is_durable_across_store_instances():
    kv = _FakeKv()
    _store(kv).put(_NS, "k", {"x": 1})
    # A fresh store instance over the SAME backend sees the persisted value.
    got = _store(kv).get(_NS, "k")
    assert got is not None and got.value == {"x": 1}


def test_a_corrupt_backend_envelope_is_rejected():
    kv = _FakeKv()
    store = _store(kv)
    store.put(_NS, "k", 1)
    corrupt_key = next(iter(kv.store))
    kv.store[corrupt_key] = "{ not json"
    with pytest.raises(GraphIntegrityError, match="corrupt"):
        store.get(_NS, "k")


def test_get_rejects_an_envelope_planted_under_the_wrong_key():
    kv = _FakeKv()
    store = _store(kv, org="org1", project="proj1")
    store.put(("a",), "k", "legit")
    backend_key = next(iter(kv.store))
    # A schema-valid envelope, but its identity (ns=["b"]) disagrees with the key it
    # sits under — get must fail closed, not return it.
    kv.store[backend_key] = json.dumps({"ns": ["b"], "key": "k", "t": "2026-01-01T00:00:00+00:00", "v": 1})
    with pytest.raises(GraphIntegrityError, match="does not match its storage key"):
        store.get(("a",), "k")


def test_search_rejects_an_entry_whose_envelope_disagrees_with_its_key():
    kv = _FakeKv()
    store = _store(kv, org="org1", project="proj1")
    store.put(("a",), "k1", 1)
    store.put(("a",), "k2", 2)
    # Corrupt k2's envelope identity while it stays listed under the ("a",) prefix.
    for backend_key in list(kv.store):
        if json.loads(kv.store[backend_key])["key"] == "k2":
            kv.store[backend_key] = json.dumps({"ns": ["b"], "key": "evil", "t": "2026-01-01T00:00:00+00:00", "v": 9})
    with pytest.raises(GraphIntegrityError, match="does not match its storage key"):
        store.search(("a",))


@pytest.mark.parametrize("envelope", [
    "{ not json",
    '{"key":"k","t":"2026-01-01T00:00:00+00:00","v":1}',              # missing ns
    '{"ns":"a","key":"k","t":"2026-01-01T00:00:00+00:00","v":1}',     # ns not a list
    '{"ns":[1],"key":"k","t":"2026-01-01T00:00:00+00:00","v":1}',     # ns part not str
    '{"ns":[],"key":"k","t":"2026-01-01T00:00:00+00:00","v":1}',      # empty ns
    '{"ns":["a"],"key":123,"t":"2026-01-01T00:00:00+00:00","v":1}',   # key not str
    '{"ns":["a"],"key":"","t":"2026-01-01T00:00:00+00:00","v":1}',    # empty key
    '{"ns":["a"],"key":"k","t":123,"v":1}',                           # t not str
    '{"ns":["a"],"key":"k","t":"2026-01-01T00:00:00+00:00"}',         # missing v
    '{"ns":["a"],"key":"k","t":"2026-01-01T00:00:00+00:00","v":NaN}', # non-finite float
])
def test_a_corrupt_or_hostile_envelope_schema_is_rejected(envelope):
    kv = _FakeKv()
    store = _store(kv, org="org1", project="proj1")
    store.put(("a",), "k", "legit")
    kv.store[next(iter(kv.store))] = envelope
    with pytest.raises(GraphIntegrityError):
        store.get(("a",), "k")


def test_an_oversize_tenant_is_refused():
    with pytest.raises(GraphValidationError, match="identifier byte cap"):
        _store(org="o" * 2000)
