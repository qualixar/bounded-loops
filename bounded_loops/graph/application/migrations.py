"""Pure, explicit migrations for portable graph-authoring documents."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping


AUTHORING_V0 = "bounded-loops.dev/graph/v0"
AUTHORING_V1 = "bounded-loops.dev/graph/v1"


def migrate_authoring_graph(document: Mapping[str, Any]) -> dict[str, Any]:
    """Return a v1 authoring document without mutating the caller's value.

    Every version transition is named and intentionally narrow.  Unknown
    versions are denied rather than guessed, so a new producer cannot acquire
    execution semantics merely by sharing a similar shape.
    """
    migrated = deepcopy(dict(document))
    version = migrated.get("api_version")
    if version == AUTHORING_V1:
        return migrated
    if version != AUTHORING_V0:
        raise ValueError(f"unsupported authoring graph API version: {version!r}")

    graph_id = migrated.pop("id", None)
    graph_version = migrated.pop("revision", None)
    if not isinstance(graph_id, str) or not graph_id:
        raise ValueError("v0 authoring graph requires a non-empty 'id'")
    if not isinstance(graph_version, str) or not graph_version:
        raise ValueError("v0 authoring graph requires a non-empty 'revision'")

    migrated["api_version"] = AUTHORING_V1
    migrated["graph_id"] = graph_id
    migrated["version"] = graph_version
    return migrated
