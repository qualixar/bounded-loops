from __future__ import annotations

import pytest

from bounded_loops.graph.application.migrations import migrate_authoring_graph
from bounded_loops.graph.application.schemas import authoring_graph_schema


def test_v0_authoring_graph_migrates_to_v1_and_schema_is_packaged():
    migrated = migrate_authoring_graph({
        "api_version": "bounded-loops.dev/graph/v0", "id": "research-brief", "revision": "1.0.0",
        "nodes": [], "edges": [], "connection_slots": [],
        "policies": {"data_class": "public", "fail_mode": "fail_closed"},
    })

    assert migrated["api_version"] == "bounded-loops.dev/graph/v1"
    assert migrated["graph_id"] == "research-brief"
    assert migrated["version"] == "1.0.0"
    assert authoring_graph_schema()["$id"].endswith("authoring-graph.schema.json")


def test_migration_is_non_mutating_and_rejects_unknown_versions():
    source = {
        "api_version": "bounded-loops.dev/graph/v0",
        "id": "research-brief",
        "revision": "1.0.0",
        "nodes": [{"nested": {"value": "unchanged"}}],
    }

    migrated = migrate_authoring_graph(source)
    migrated["nodes"][0]["nested"]["value"] = "changed"

    assert source["nodes"][0]["nested"]["value"] == "unchanged"
    with pytest.raises(ValueError, match="unsupported authoring graph API version"):
        migrate_authoring_graph({"api_version": "bounded-loops.dev/graph/v99"})
