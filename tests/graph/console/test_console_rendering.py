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


# ── spend, and the stop-and-decide card ───────────────────────────────────────
# A budget pause is a decision point. The console is the surface for an operator who does not
# use a CLI, so it has to say what was spent, what was authorised, and offer a new number.


def _paused(**overrides: object) -> ArenaProjection:
    pause: dict[str, object] = {
        "node_id": "worker", "attempt": 3, "tokens": 200, "cost_microunits": 2_400,
        "max_tokens": 150,
        "reason": "this run token budget is spent: 200 of 150 consumed, so no further "
                  "attempt may start",
    }
    pause.update(overrides)
    return ArenaProjection(
        organization_id="local-org", project_id="local-project", run_id="run-1",
        graph_digest="sha256:" + "a" * 64, plan_digest="sha256:" + "b" * 64,
        policy_digest="sha256:" + "c" * 64, run_state="RUNNING",
        receipt_sequence=9, receipt_head_hash="0" * 64,
        nodes=(_node(node_id="worker", state="GATING"),), edges=(), levels=(("worker",),),
        budget_pause=pause, spend_tokens=200, spend_cost_microunits=2_400,
        spend_complete=True,
    )


def test_a_paused_run_shows_what_it_spent_and_offers_a_new_limit() -> None:
    html = render_console_page(
        identity=_IDENTITY, projection=_paused(), token="tok123", notice=None,
    )

    assert "Paused" in html and "spending limit reached" in html
    assert "200" in html and "$0.002400" in html
    assert "token ceiling <strong>150</strong>" in html
    # The form is the point: one number, one button, no CLI and no manifest edit.
    assert 'action="/continue"' in html
    assert 'name="max_tokens"' in html
    assert 'name="max_cost_usd"' in html
    assert 'value="tok123"' in html, "the CSRF token must ride the form"
    assert "LOWER number stops the run sooner" in html


def test_an_incomplete_total_is_shown_as_a_floor() -> None:
    """"200 tokens" and "at least 200 tokens" support different decisions."""
    html = render_console_page(
        identity=_IDENTITY,
        projection=ArenaProjection(
            organization_id="local-org", project_id="local-project", run_id="run-1",
            graph_digest="sha256:" + "a" * 64, plan_digest="sha256:" + "b" * 64,
            policy_digest="sha256:" + "c" * 64, run_state="RUNNING",
            receipt_sequence=3, receipt_head_hash="0" * 64,
            nodes=(_node(),), edges=(), levels=(("checkpoint",),),
            spend_tokens=200, spend_cost_microunits=0, spend_complete=False,
        ),
        token="t", notice=None,
    )

    assert "Spent at least" in html


def test_a_run_that_never_paused_shows_no_limit_card() -> None:
    html = render_console_page(
        identity=_IDENTITY, projection=_projection(nodes=(_node(),)), token="t", notice=None,
    )

    assert "spending limit reached" not in html
    assert 'action="/continue"' not in html
    assert "No spend measured yet." in html


def test_a_finished_run_does_not_ask_for_a_new_limit() -> None:
    """A terminal run can spend nothing more, so asking would be noise."""
    finished = _paused()
    html = render_console_page(
        identity=_IDENTITY,
        projection=ArenaProjection(
            **{**finished.__dict__, "run_state": "SUCCEEDED"},
        ),
        token="t", notice=None,
    )

    assert 'action="/continue"' not in html
    # The spend itself is still reported — that is history worth reading.
    assert "200" in html
