#!/usr/bin/env python3
"""Regenerate the shipped reference graphs under ``graphs/`` from the loop catalogue.

A ``kind: loop`` node pins its package by CONTENT digest, which is the property that makes a run
reproducible — and also means a committed graph manifest goes stale the moment its loop package
changes. Rather than leave that to be discovered at run time, the digests are generated here and a
test asserts the committed files still match the current packages. Drift then fails in CI with a
pointer to this script, instead of surfacing as ``package digest is not admitted`` on someone's
machine.

Run it after changing any loop package a reference graph uses:

    uv run python scripts/regenerate_reference_graphs.py

The graph SHAPES live in ``bounded_loops/graph/reference_graphs.py`` so the test and this script read
one definition rather than two that agree by luck.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from bounded_loops.graph.reference_graphs import (  # noqa: E402  (path set above)
    REFERENCE_GRAPHS,
    graphs_root,
    render_reference_graph,
)


def main() -> int:
    root = graphs_root(REPO_ROOT)
    root.mkdir(parents=True, exist_ok=True)
    for definition in REFERENCE_GRAPHS:
        target = root / definition.slug / "graph.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_reference_graph(definition, REPO_ROOT), encoding="utf-8")
        print(f"wrote {target.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
