"""Starter graphs for the Graph Studio — one per persona plus the runnable demo.

Each template is an authoring-graph mapping in the exact shape
``validate_graph`` accepts (so the Studio can seed, edit, and re-export it, and
`bl graph lint` validates the export). Every template is unbound (no connection
slots) so it compiles without connectors; the nodes that call external models
(research, publish) are authorable and validate now but only EXECUTE once a
connector is admitted (C1) — the Studio states this honestly rather than
implying a content pipeline runs end-to-end today.
"""

from __future__ import annotations

from typing import Any

_API = "bounded-loops.dev/graph/v1"


def _budget(attempts: int = 1, wallclock_s: int = 30) -> dict[str, int]:
    return {"max_attempts": attempts, "max_wallclock_s": wallclock_s}


# ── runnable-now demo (mirrors application/sandbox_demo) ────────────────────────

_SANDBOX_DEMO: dict[str, Any] = {
    "api_version": _API,
    "graph_id": "sandbox-demo",
    "version": "1.0.0",
    "nodes": [
        {
            "id": "sandbox_probe",
            "kind": "tool",
            "tool_ref": "local:sandbox-probe",
            "inputs": {},
            "outputs": {"result": "json"},
            "budget": _budget(1, 15),
            "effects": ["workspace_write"],
            "isolation": "container_restricted",
        }
    ],
    "edges": [],
    "connection_slots": [],
    "policies": {"data_class": "internal", "fail_mode": "fail_closed"},
}

# ── content creator: blog pipeline ──────────────────────────────────────────────

_BLOG_PIPELINE: dict[str, Any] = {
    "api_version": _API,
    "graph_id": "blog-pipeline",
    "version": "1.0.0",
    "nodes": [
        {
            "id": "research", "kind": "research_source", "source_policy": "web-allowlisted",
            "inputs": {}, "outputs": {"sources": "json"}, "budget": _budget(),
            "effects": ["read_only"], "isolation": "workspace_only",
        },
        {
            "id": "claims", "kind": "research_claim",
            "inputs": {"sources": "json"}, "outputs": {"claims": "json"}, "budget": _budget(),
            "effects": ["read_only"], "isolation": "workspace_only",
        },
        {
            "id": "draft", "kind": "tool", "tool_ref": "model:draft-writer",
            # 2 attempts: drafting is the node most likely to be rejected by its gate on a
            # first pass, and the controller now honours the budget.
            "inputs": {"claims": "json"}, "outputs": {"draft": "text"}, "budget": _budget(2, 120),
            "effects": ["workspace_write"], "isolation": "workspace_only",
        },
        {
            "id": "review", "kind": "audit", "audit_profile": "editorial-v1",
            "inputs": {"draft": "text"}, "outputs": {"reviewed": "text"}, "budget": _budget(),
            "effects": ["read_only"], "isolation": "workspace_only",
        },
        {
            "id": "approve", "kind": "approval", "required_role": "editor",
            "inputs": {"reviewed": "text"}, "outputs": {"approved": "text"}, "budget": _budget(),
            "effects": ["read_only"], "isolation": "workspace_only",
        },
        {
            "id": "publish", "kind": "publish", "publication_policy": "blog-cms",
            "inputs": {"approved": "text"}, "outputs": {"url": "text"}, "budget": _budget(),
            "effects": ["external_write"], "isolation": "container_restricted",
        },
    ],
    "edges": [
        {"from_node": "research", "from_port": "sources", "to_node": "claims", "to_port": "sources", "when": None},
        {"from_node": "claims", "from_port": "claims", "to_node": "draft", "to_port": "claims", "when": None},
        {"from_node": "draft", "from_port": "draft", "to_node": "review", "to_port": "draft", "when": None},
        {"from_node": "review", "from_port": "reviewed", "to_node": "approve", "to_port": "reviewed", "when": None},
        {"from_node": "approve", "from_port": "approved", "to_node": "publish", "to_port": "approved", "when": None},
    ],
    "connection_slots": [],
    "policies": {"data_class": "internal", "fail_mode": "fail_closed"},
}

# ── developer: code-review pipeline ─────────────────────────────────────────────

_CODE_REVIEW: dict[str, Any] = {
    "api_version": _API,
    "graph_id": "code-review-pipeline",
    "version": "1.0.0",
    "nodes": [
        {
            "id": "fetch_diff", "kind": "tool", "tool_ref": "local:git-diff",
            "inputs": {}, "outputs": {"diff": "text"}, "budget": _budget(),
            "effects": ["read_only"], "isolation": "workspace_only",
        },
        {
            "id": "analyze", "kind": "tool", "tool_ref": "local:static-analysis",
            "inputs": {"diff": "text"}, "outputs": {"findings": "json"}, "budget": _budget(1, 60),
            "effects": ["workspace_write"], "isolation": "container_restricted",
        },
        {
            "id": "security_audit", "kind": "audit", "audit_profile": "security-v1",
            "inputs": {"findings": "json"}, "outputs": {"report": "json"}, "budget": _budget(),
            "effects": ["read_only"], "isolation": "workspace_only",
        },
        {
            "id": "approve_merge", "kind": "approval", "required_role": "maintainer",
            "inputs": {"report": "json"}, "outputs": {"decision": "json"}, "budget": _budget(),
            "effects": ["read_only"], "isolation": "workspace_only",
        },
    ],
    "edges": [
        {"from_node": "fetch_diff", "from_port": "diff", "to_node": "analyze", "to_port": "diff", "when": None},
        {"from_node": "analyze", "from_port": "findings", "to_node": "security_audit", "to_port": "findings", "when": None},
        {"from_node": "security_audit", "from_port": "report", "to_node": "approve_merge", "to_port": "report", "when": None},
    ],
    "connection_slots": [],
    "policies": {"data_class": "internal", "fail_mode": "fail_closed"},
}


STARTER_TEMPLATES: tuple[dict[str, Any], ...] = (
    {
        "id": "sandbox-demo",
        "label": "Sandbox demo (runs now, no Docker)",
        "persona": "everyone",
        "description": "One local tool node that runs for real inside a native OS sandbox. Use it to see execution + the Arena end to end.",
        "runnable_now": True,
        "spec": _SANDBOX_DEMO,
    },
    {
        "id": "blog-pipeline",
        "label": "Blog pipeline (content creator)",
        "persona": "content-creator",
        "description": "Research → claims → draft → editorial review → approval → publish. Design and validate it now; the research/draft/publish nodes run once a connector is admitted.",
        "runnable_now": False,
        "spec": _BLOG_PIPELINE,
    },
    {
        "id": "code-review-pipeline",
        "label": "Code-review pipeline (developer)",
        "persona": "developer",
        "description": "Fetch diff → static analysis → security audit → maintainer approval. A gated review DAG with an independent audit node.",
        "runnable_now": False,
        "spec": _CODE_REVIEW,
    },
)
