"""Memory spine (ADR-12 D3) — tenant-scoped, namespaced, JSON-only working memory.

Agent working memory, never authority (the event log is truth). The store is tenant-
bound by construction, returns an independent copy on every read (stored memory is
immutable to callers), and fails closed on a non-serializable / oversize value, an
empty namespace or key, or a missing tenant.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from bounded_loops.graph.application.memory_store import InMemoryGraphMemoryStore
from bounded_loops.graph.domain.errors import GraphValidationError

_NS = ("memories",)


def _store(**over):
    kwargs = dict(organization_id="org-1", project_id="project-1")
    kwargs.update(over)
    return InMemoryGraphMemoryStore(**kwargs)


def test_put_then_get_roundtrips_the_value():
    store = _store(clock=lambda: datetime(2026, 8, 10, tzinfo=timezone.utc))
    record = store.put(_NS, "k", {"b": 2, "a": [1, 2, 3]})

    assert record.namespace == _NS and record.key == "k"
    got = store.get(_NS, "k")
    assert got is not None and got.value == {"b": 2, "a": [1, 2, 3]}
    assert got.updated_at == "2026-08-10T00:00:00+00:00"


def test_get_miss_returns_none():
    assert _store().get(_NS, "absent") is None


def test_put_overwrites_the_prior_value():
    store = _store()
    store.put(_NS, "k", "first")
    store.put(_NS, "k", "second")
    got = store.get(_NS, "k")
    assert got is not None and got.value == "second"


def test_search_returns_only_the_namespace_sorted_by_key():
    store = _store()
    store.put(_NS, "b", 2)
    store.put(_NS, "a", 1)
    store.put(("other",), "x", 99)  # a different namespace must not leak in

    records = store.search(_NS)
    assert [record.key for record in records] == ["a", "b"]
    assert [record.value for record in records] == [1, 2]


def test_delete_reports_whether_it_removed_anything():
    store = _store()
    store.put(_NS, "k", 1)
    assert store.delete(_NS, "k") is True
    assert store.get(_NS, "k") is None
    assert store.delete(_NS, "k") is False


def test_a_non_serializable_value_is_refused():
    with pytest.raises(GraphValidationError, match="JSON-serializable"):
        _store().put(_NS, "k", {1, 2, 3})  # a set is not JSON-serializable


def test_an_oversize_value_is_refused():
    store = _store(max_value_bytes=16)
    with pytest.raises(GraphValidationError, match="byte cap"):
        store.put(_NS, "k", "x" * 100)


@pytest.mark.parametrize("namespace", [(), ("",), ("   ",), "notatuple"])
def test_an_invalid_namespace_is_refused(namespace):
    with pytest.raises(GraphValidationError, match="namespace"):
        _store().put(namespace, "k", 1)  # type: ignore[arg-type]


def test_an_empty_key_is_refused():
    with pytest.raises(GraphValidationError, match="key"):
        _store().put(_NS, "", 1)


def test_a_whitespace_only_key_is_refused():
    with pytest.raises(GraphValidationError, match="key"):
        _store().put(_NS, "   ", 1)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_nan_and_infinity_are_refused(bad):
    with pytest.raises(GraphValidationError, match="JSON-serializable"):
        _store().put(_NS, "k", bad)


def test_an_oversize_key_is_refused():
    with pytest.raises(GraphValidationError, match="identifier byte cap"):
        _store().put(_NS, "k" * 2000, 1)


def test_an_oversize_namespace_part_is_refused():
    with pytest.raises(GraphValidationError, match="identifier byte cap"):
        _store().put(("n" * 2000,), "k", 1)


def test_a_whitespace_only_tenant_is_refused():
    with pytest.raises(GraphValidationError, match="organization and project"):
        InMemoryGraphMemoryStore(organization_id="   ", project_id="project-1")


def test_a_store_requires_a_tenant():
    with pytest.raises(GraphValidationError, match="organization and project"):
        InMemoryGraphMemoryStore(organization_id="", project_id="project-1")


def test_two_tenants_do_not_share_memory():
    a = _store(organization_id="org-a")
    b = _store(organization_id="org-b")
    a.put(_NS, "k", "a-secret")

    # Same namespace + key, different tenant store — no leak.
    assert b.get(_NS, "k") is None
    assert a.tenant == ("org-a", "project-1") and b.tenant == ("org-b", "project-1")


def test_mutating_a_read_value_does_not_change_stored_memory():
    store = _store()
    store.put(_NS, "k", {"items": [1]})

    first = store.get(_NS, "k")
    assert first is not None
    first.value["items"].append(999)  # type: ignore[index]  # mutate the returned copy

    second = store.get(_NS, "k")
    assert second is not None and second.value == {"items": [1]}  # store is unchanged
