from __future__ import annotations

import json
import re

import pytest

from bounded_loops.graph.arena.render import render_arena_html, load_template


def _projection(**overrides):
    base = {
        "organization_id": "org-1",
        "project_id": "project-1",
        "run_id": "run-1",
        "graph_digest": "sha256:" + "0" * 64,
        "plan_digest": "sha256:" + "1" * 64,
        "policy_digest": "sha256:" + "2" * 64,
        "run_state": "SUCCEEDED",
        "receipt_sequence": 7,
        "receipt_head_hash": "sha256:" + "3" * 64,
        "nodes": [
            {
                "node_id": "research",
                "kind": "research_claim",
                "state": "SUCCEEDED",
                "attempt": 1,
                "required_effects": ["read_only"],
                "isolation": "workspace_only",
                "hard_deadline_ms": 60000,
                "artifact_digests": ["sha256:" + "a" * 64],
                "route": None,
                "transport": None,
            }
        ],
        "edges": [],
        "levels": [["research"]],
    }
    base.update(overrides)
    return base


def _extract_arena_json(html: str) -> str:
    match = re.search(
        r'<script id="arena-data" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    assert match, "arena-data block not found"
    return match.group(1).strip()


def test_renders_projection_into_parseable_data_block():
    html = render_arena_html(_projection())
    assert html.lstrip().startswith("<!DOCTYPE html>")
    # The embedded demo run-4823 must be gone; our projection must be present.
    assert "run-4823" not in _extract_arena_json(html)
    data = json.loads(_extract_arena_json(html))
    assert data["run_id"] == "run-1"
    assert data["nodes"][0]["node_id"] == "research"


def test_hostile_receipt_content_cannot_break_out_of_the_script_block():
    hostile = _projection(run_id="</script><script>window.__pwned=1</script>")
    html = render_arena_html(hostile)
    # No raw closing tag may appear inside the injected data block.
    block = _extract_arena_json(html)
    assert "</script>" not in block
    assert "\\u003c/script\\u003e" in block or "\\u003c" in block
    # And it must still round-trip back to the exact hostile string.
    data = json.loads(block)
    assert data["run_id"] == "</script><script>window.__pwned=1</script>"


def test_template_has_both_injection_markers():
    template = load_template()
    assert '<script id="arena-data" type="application/json">' in template
    assert template.count("</script>") >= 2  # arena-data block + the renderer script


def test_missing_markers_raise():
    with pytest.raises(ValueError):
        render_arena_html(_projection(), template="<html>no markers</html>")
