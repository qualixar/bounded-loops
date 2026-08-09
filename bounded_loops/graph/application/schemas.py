"""Load versioned graph schemas from package resources."""

from __future__ import annotations

from importlib.resources import files
import json
from typing import Any


def authoring_graph_schema() -> dict[str, Any]:
    """Return a fresh v1 authoring schema from the installed distribution."""
    resource = files("bounded_loops.graph.schemas").joinpath("authoring-graph.schema.json")
    return json.loads(resource.read_text(encoding="utf-8"))
