"""RED-first tests for `bounded_loops.graph.console.rendering` (Slice 3).

Pure-function tests, no networking, no filesystem run directory — just
`ArenaProjection` values fed straight into the renderer. This is the ONE place
untrusted/echoed values (node ids, tokens, query-string notices) are
interpolated into HTML, so its escaping behavior is tested directly and in
isolation from the HTTP layer.
"""

from __future__ import annotations

import pytest

from bounded_loops.graph.application.arena_projection import (
    ArenaNodeProjection,
    ArenaProjection,
)
from bounded_loops.graph.console.rendering import load_template, render_console_page
from bounded_loops.graph.domain.events import GraphRunIdentity

_IDENTITY = GraphRunIdentity(
    organization_id="local-org", project_id="local-project", run_id="run-1",
    graph_digest="sha256:" + "a" * 64, plan_digest="sha256:" + "b" * 64,
    policy_digest="sha256:" + "c" * 64,
)


def _node(node_id: str = "checkpoint", state: str = "AWAITING_APPROVAL") -> ArenaNodeProjection:
    return ArenaNodeProjection(
        node_id=node_id, kind="approval", state=state, attempt=1,
        required_effects=("read_only",), isolation="workspace_only", hard_deadline_ms=30_000,
        artifact_digests=(), route=None, transport=None,
    )


def _projection(*, nodes: tuple[ArenaNodeProjection, ...], run_state: str = "RUNNING") -> ArenaProjection:
    return ArenaProjection(
        organization_id="local-org", project_id="local-project", run_id="run-1",
        graph_digest="sha256:" + "a" * 64, plan_digest="sha256:" + "b" * 64,
        policy_digest="sha256:" + "c" * 64, run_state=run_state,
        receipt_sequence=1, receipt_head_hash="0" * 64,
        nodes=nodes, edges=(), levels=(tuple(n.node_id for n in nodes),),
    )


def test_load_template_contains_the_body_marker() -> None:
    assert "<!--CONSOLE-BODY-->" in load_template()


def test_render_console_page_shows_run_identity_and_awaiting_node() -> None:
    projection = _projection(nodes=(_node(),))
    html = render_console_page(identity=_IDENTITY, projection=projection, token="tok123", notice=None)
    assert "run-1" in html
    assert "local-org" in html
    assert "checkpoint" in html
    assert '<form method="post" action="/approve">' in html
    assert '<form method="post" action="/reject">' in html
    assert 'name="token" value="tok123"' in html
    assert 'name="node_id" value="checkpoint"' in html


def test_render_console_page_shows_node_kind_effects_and_isolation() -> None:
    projection = _projection(nodes=(_node(),))
    html = render_console_page(identity=_IDENTITY, projection=projection, token="tok123", notice=None)
    assert "approval" in html
    assert "read_only" in html
    assert "workspace_only" in html


def test_render_console_page_empty_state_when_nothing_is_awaiting() -> None:
    projection = _projection(nodes=(_node(state="SUCCEEDED"),), run_state="SUCCEEDED")
    html = render_console_page(identity=_IDENTITY, projection=projection, token="tok123", notice=None)
    assert "Nothing is currently awaiting approval" in html
    assert "SUCCEEDED" in html
    assert "<form" not in html


def test_render_console_page_includes_the_notice_when_provided() -> None:
    projection = _projection(nodes=(_node(state="SUCCEEDED"),), run_state="SUCCEEDED")
    html = render_console_page(
        identity=_IDENTITY, projection=projection, token="tok123",
        notice="node 'checkpoint' decision: approved",
    )
    assert "decision: approved" in html


def test_render_console_page_escapes_a_hostile_node_id() -> None:
    hostile = _node(node_id='</script><img src=x onerror=alert(1)>')
    projection = _projection(nodes=(hostile,))
    html = render_console_page(identity=_IDENTITY, projection=projection, token="tok123", notice=None)
    assert "<script>" not in html
    assert "onerror=alert(1)>" not in html
    assert "&lt;script&gt;" in html or "&lt;/script&gt;" in html


def test_render_console_page_escapes_quotes_in_the_token_attribute() -> None:
    projection = _projection(nodes=(_node(),))
    hostile_token = 'abc"><script>alert(1)</script>'
    html = render_console_page(identity=_IDENTITY, projection=projection, token=hostile_token, notice=None)
    assert "<script>alert(1)</script>" not in html
    assert "&quot;" in html


def test_render_console_page_escapes_a_hostile_notice() -> None:
    projection = _projection(nodes=(_node(state="SUCCEEDED"),), run_state="SUCCEEDED")
    html = render_console_page(
        identity=_IDENTITY, projection=projection, token="tok123",
        notice="<script>alert(1)</script>",
    )
    assert "<script>alert(1)</script>" not in html


def test_render_console_page_documents_the_local_trust_posture() -> None:
    projection = _projection(nodes=(_node(),))
    html = render_console_page(identity=_IDENTITY, projection=projection, token="tok123", notice=None)
    lowered = html.lower()
    assert "local" in lowered
    assert "hosted" in lowered


def test_render_console_page_raises_if_template_is_missing_the_marker() -> None:
    projection = _projection(nodes=(_node(),))
    with pytest.raises(ValueError, match="marker"):
        render_console_page(
            identity=_IDENTITY, projection=projection, token="tok123", notice=None,
            template="<html><body>no marker here</body></html>",
        )


def test_render_console_page_only_lists_nodes_currently_awaiting_approval() -> None:
    projection = _projection(
        nodes=(_node(node_id="done", state="SUCCEEDED"), _node(node_id="pending")),
    )
    html = render_console_page(identity=_IDENTITY, projection=projection, token="tok123", notice=None)
    assert "pending" in html
    # "done" must not get its own Approve/Reject form (it already resolved).
    assert 'name="node_id" value="done"' not in html
