"""Graph MCP surface — thin shim over an injected GraphRuntimeFacade (task #4). Tenant-scoped,
read tools side-effect-free, mutating tools gated by a server-side confirm, errors shaped."""

from __future__ import annotations

from bounded_loops.graph.application.arena_projection import ArenaNodeProjection, ArenaProjection
from bounded_loops.graph.domain.errors import GraphIntegrityError, GraphValidationError
from bounded_loops.graph.mcp_graph import (
    graph_approve,
    graph_resume,
    graph_state_md,
    graph_status,
    register,
)


def _proj() -> ArenaProjection:
    node = ArenaNodeProjection(
        node_id="a", kind="worker", state="SUCCEEDED", attempt=1,
        required_effects=("read_only",), isolation="native", hard_deadline_ms=1000,
        artifact_digests=("sha256:aa",), route=None, transport=None,
    )
    return ArenaProjection(
        organization_id="o", project_id="p", run_id="run-1",
        graph_digest="g", plan_digest="pl", policy_digest="po", run_state="SUCCEEDED",
        receipt_sequence=3, receipt_head_hash="h", nodes=(node,), edges=(), levels=(("a",),),
    )


def _ok_payload() -> dict:
    return {"subject_id": "s", "organization_id": "o", "project_id": "p", "run_id": "run-1"}


class _FakeFacade:
    def __init__(self, projection: ArenaProjection | None = None, error: Exception | None = None) -> None:
        self._projection = projection
        self._error = error
        self.calls: list = []

    def status(self, request):
        self.calls.append(("status", request))
        if self._error is not None:
            raise self._error
        return self._projection

    def resume(self, request):
        self.calls.append(("resume", request))
        if self._error is not None:
            raise self._error
        return self._projection

    def approve(self, request, *, node_id, decision):
        self.calls.append(("approve", request, node_id, decision))
        if self._error is not None:
            raise self._error
        return self._projection


def test_graph_status_returns_the_projection_dict():
    facade = _FakeFacade(_proj())
    out = graph_status(facade, _ok_payload())
    assert out["ok"] is True
    assert out["projection"]["run_id"] == "run-1"
    assert out["projection"]["nodes"][0]["node_id"] == "a"
    assert facade.calls[0][0] == "status"


def test_graph_state_md_renders_the_state_document():
    out = graph_state_md(_FakeFacade(_proj()), _ok_payload())
    assert out["ok"] is True
    assert "# Run STATE — run-1" in out["state_md"]


def test_resume_requires_confirm():
    facade = _FakeFacade(_proj())
    preview = graph_resume(facade, _ok_payload())  # confirm defaults False
    assert preview["ok"] is True and preview["preview"] is True
    assert facade.calls == []  # nothing mutated on a preview
    done = graph_resume(facade, _ok_payload(), confirm=True)
    assert done["ok"] is True and "projection" in done
    assert facade.calls[0][0] == "resume"


def test_approve_requires_confirm_and_validates_decision():
    facade = _FakeFacade(_proj())
    preview = graph_approve(facade, _ok_payload(), node_id="a", decision="approved")
    assert preview["preview"] is True and facade.calls == []
    done = graph_approve(facade, _ok_payload(), node_id="a", decision="approved", confirm=True)
    assert done["ok"] is True and facade.calls[0][0] == "approve"
    assert facade.calls[0][2] == "a" and facade.calls[0][3] == "approved"
    bad = graph_approve(facade, _ok_payload(), node_id="a", decision="maybe", confirm=True)
    assert bad["ok"] is False and bad["error"]["pointer"] == "/decision"


def test_missing_tenant_field_is_a_validation_error_before_the_facade():
    facade = _FakeFacade(_proj())
    out = graph_status(facade, {"subject_id": "s", "organization_id": "o", "project_id": "p", "run_id": ""})
    assert out["ok"] is False and out["error"]["pointer"] == "/run_id"
    assert facade.calls == []  # never reached the facade


def test_a_facade_validation_error_is_shaped():
    facade = _FakeFacade(error=GraphValidationError("arena", "/reader", "unauthorized"))
    out = graph_status(facade, _ok_payload())
    assert out["ok"] is False
    assert out["error"] == {"code": "arena", "pointer": "/reader", "message": "unauthorized"}


def test_a_facade_integrity_error_is_shaped():
    facade = _FakeFacade(error=GraphIntegrityError("receipt stream corrupt"))
    out = graph_status(facade, _ok_payload())
    assert out["ok"] is False and out["error"]["code"] == "graph_integrity"
    assert "corrupt" in out["error"]["message"]


class _FakeMcp:
    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def test_register_injects_the_authenticated_subject_not_an_llm_arg():
    facade = _FakeFacade(_proj())
    mcp = _FakeMcp()
    register(mcp, facade, subject_provider=lambda: "authenticated-subject")
    assert set(mcp.tools) == {"graph_status_tool", "graph_state_md_tool", "graph_resume_tool", "graph_approve_tool"}
    # subject_id is NOT an LLM tool arg — the tools take only intent (org/project/run)
    out = mcp.tools["graph_status_tool"](organization_id="o", project_id="p", run_id="run-1")
    assert out["ok"] is True and out["projection"]["run_id"] == "run-1"
    # the subject the facade sees came from subject_provider (the session), not the caller
    assert facade.calls[-1][1].subject_id == "authenticated-subject"
    # a mutating tool still honors the confirm gate through the wiring
    assert mcp.tools["graph_resume_tool"](organization_id="o", project_id="p", run_id="run-1")["preview"] is True
