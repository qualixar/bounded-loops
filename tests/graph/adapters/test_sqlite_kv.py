"""SqliteDurableKeyValue — a durable, correct exact-KV backing DurableKeyValuePort.

Proves durability across instances (the point of "durable") and that prefix listing is
a LITERAL prefix match, not a LIKE wildcard (a netstring key can contain '_' / '%'),
and that it composes with KeyValueBackedMemoryStore into a durable memory spine.
"""

from __future__ import annotations

import pytest

from bounded_loops.graph.adapters.persistence.sqlite_kv import SqliteDurableKeyValue
from bounded_loops.graph.application.memory_store import KeyValueBackedMemoryStore
from bounded_loops.graph.domain.errors import GraphIntegrityError


def test_set_get_overwrite_delete(tmp_path):
    kv = SqliteDurableKeyValue(tmp_path / "m.db")
    assert kv.get("k") is None
    kv.set("k", "v1")
    assert kv.get("k") == "v1"
    kv.set("k", "v2")
    assert kv.get("k") == "v2"
    assert kv.delete("k") is True
    assert kv.get("k") is None
    assert kv.delete("k") is False


def test_list_prefix_is_a_literal_prefix_not_a_like_wildcard(tmp_path):
    kv = SqliteDurableKeyValue(tmp_path / "m.db")
    kv.set("a_bc", "1")   # '_' is a LIKE wildcard — must match literally
    kv.set("aXbc", "2")   # would wrongly match "a_b%" under LIKE
    kv.set("a%z", "3")    # '%' is a LIKE wildcard
    kv.set("other", "9")
    assert dict(kv.list_prefix("a_b")) == {"a_bc": "1"}
    assert dict(kv.list_prefix("a%")) == {"a%z": "3"}
    assert kv.list_prefix("zzz") == ()


def test_durable_across_instances(tmp_path):
    path = tmp_path / "m.db"
    SqliteDurableKeyValue(path).set("k", "persisted")
    # A brand-new instance over the same file sees the write — that is "durable".
    assert SqliteDurableKeyValue(path).get("k") == "persisted"


def test_refuses_a_symlink_path(tmp_path):
    real = tmp_path / "real.db"
    real.write_text("")
    link = tmp_path / "link.db"
    link.symlink_to(real)
    with pytest.raises(GraphIntegrityError, match="symlink"):
        SqliteDurableKeyValue(link)


def test_backs_a_durable_memory_spine_across_instances(tmp_path):
    path = tmp_path / "m.db"
    store_a = KeyValueBackedMemoryStore(
        organization_id="org1", project_id="proj1", kv=SqliteDurableKeyValue(path),
    )
    store_a.put(("notes",), "k", {"x": 1})

    # A fresh store + fresh SQLite handle over the same file recalls it durably.
    store_b = KeyValueBackedMemoryStore(
        organization_id="org1", project_id="proj1", kv=SqliteDurableKeyValue(path),
    )
    got = store_b.get(("notes",), "k")
    assert got is not None and got.value == {"x": 1}
    assert [record.key for record in store_b.search(("notes",))] == ["k"]

    # Tenant isolation still holds over the shared durable backend.
    other = KeyValueBackedMemoryStore(
        organization_id="org2", project_id="proj1", kv=SqliteDurableKeyValue(path),
    )
    assert other.get(("notes",), "k") is None
