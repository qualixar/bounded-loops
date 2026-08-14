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


# ── loop node evidence ─────────────────────────────────────────────────────────

def _loop_projection(**node_overrides) -> dict:
    """A projection with one kind:loop node, optionally carrying loop_meta."""
    node: dict = {
        "node_id": "run-loop",
        "kind": "loop",
        "state": "SUCCEEDED",
        "attempt": 1,
        "required_effects": ["read_only"],
        "isolation": "workspace_only",
        "hard_deadline_ms": None,
        "artifact_digests": ["sha256:" + "b" * 64],
        "route": None,
        "transport": None,
    }
    node.update(node_overrides)
    return _projection(nodes=[node])


def test_loop_node_without_loop_meta_renders_normally():
    """A loop node that has no loop_meta (old render path) must still render."""
    html = render_arena_html(_loop_projection())
    data = json.loads(_extract_arena_json(html))
    assert data["nodes"][0]["kind"] == "loop"
    # No loop_meta in the payload — the template must handle its absence silently.
    assert "loop_meta" not in data["nodes"][0]


def test_loop_node_with_package_name_is_present_in_payload():
    """loop_meta is round-tripped verbatim through the data block."""
    proj = _loop_projection()
    proj["nodes"][0]["loop_meta"] = {
        "package_digest": "sha256:" + "c" * 64,
        "package_name": "json-config-schema",
        "package_description": "Drive an agent until JSON matches a schema.",
        "loop_outcome": {
            "status": "DONE",
            "reason": "gate: all checks passed",
            "inner_ledger_digest": "d" * 64,
            "attempt": 1,
            "repair_round": 0,
        },
    }
    html = render_arena_html(proj)
    data = json.loads(_extract_arena_json(html))
    lm = data["nodes"][0]["loop_meta"]
    assert lm["package_name"] == "json-config-schema"
    assert lm["loop_outcome"]["status"] == "DONE"
    assert lm["loop_outcome"]["inner_ledger_digest"] == "d" * 64


def test_loop_evidence_LOCAL_UNVERIFIED_notice_is_in_template():
    """The HONESTY RULE: LOCAL/UNVERIFIED notice must be present in the template JS."""
    from bounded_loops.graph.arena.render import load_template
    template = load_template()
    assert "LOCAL/UNVERIFIED" in template


def test_loop_evidence_gate_passing_caveat_is_in_template():
    """Gate passing does NOT mean semantic correctness — must be stated explicitly."""
    from bounded_loops.graph.arena.render import load_template
    template = load_template()
    # Must contain a clear statement that gate = mechanical check, not semantic guarantee
    assert "mechanical check" in template or "semantically correct" in template


def test_loop_meta_with_hostile_package_name_cannot_break_out():
    """loop_meta content must be escaped through the same route as all other data."""
    hostile_name = "</script><script>window.__xss=1</script>"
    proj = _loop_projection()
    proj["nodes"][0]["loop_meta"] = {
        "package_digest": "sha256:" + "e" * 64,
        "package_name": hostile_name,
    }
    html = render_arena_html(proj)
    block = _extract_arena_json(html)
    # The raw tag must not appear verbatim in the data block
    assert "</script><script>" not in block
    # But the data must survive the round-trip unchanged
    data = json.loads(block)
    assert data["nodes"][0]["loop_meta"]["package_name"] == hostile_name


def test_a_loop_node_with_no_receipt_says_so_rather_than_showing_a_bare_badge():
    """The absence of a loop receipt is evidence, and it is the most misreadable state here.

    Before this, a FAILED loop node rendered the package badge and nothing else, because the
    outcome artifact was only read for SUCCEEDED nodes. A badge with no outcome reads as "the loop
    ran and something went wrong" when the usual truth is that the loop never launched — which is a
    different incident with a different fix. Flagged by the implementation's own review as the
    single most important thing missing during incident review.
    """
    from bounded_loops.graph.arena.render import render_arena_html

    html = render_arena_html({
        "run_id": "r1", "run_state": "FAILED", "edges": [],
        "nodes": [{
            "node_id": "validate", "kind": "loop", "state": "FAILED", "attempts": 1,
            "artifact_digests": [],
            "loop_meta": {
                "package_digest": "sha256:" + "a" * 64,
                "package_name": "json-config-schema",
                "no_receipt_reason": (
                    "no loop receipt was promoted for this node, so the loop did not reach the "
                    "point of writing one — this is a node-level failure BEFORE the loop ran, not "
                    "a loop that ran and was rejected"
                ),
            },
        }],
    })

    assert "no_receipt_reason" in html
    assert "node-level failure BEFORE the loop ran" in html
    # And the package line must not claim those bytes executed.
    assert "pins the exact bytes that ran" not in html
    assert "compiled against" in html
