"""STATE.md renderer — a pure, deterministic, injection-safe Markdown projection of a verified
ArenaProjection (ADR-12 D4: UX, never authority)."""

from __future__ import annotations

from bounded_loops.graph.application.arena_projection import ArenaNodeProjection, ArenaProjection
from bounded_loops.graph.application.state_document import render_state_markdown


def _node(
    node_id: str,
    state: str,
    *,
    kind: str = "worker",
    attempt: int = 1,
    required_effects: tuple[str, ...] = (),
    isolation: str = "native",
    hard_deadline_ms: int = 1000,
    artifact_digests: tuple[str, ...] = (),
    route: tuple[str, str, str, bool, str] | None = None,
    transport: str | None = None,
) -> ArenaNodeProjection:
    return ArenaNodeProjection(
        node_id=node_id, kind=kind, state=state, attempt=attempt,
        required_effects=required_effects, isolation=isolation,
        hard_deadline_ms=hard_deadline_ms, artifact_digests=artifact_digests,
        route=route, transport=transport,
    )


def _proj(nodes, *, levels, run_state="RUNNING", edges=()):
    return ArenaProjection(
        organization_id="org-1", project_id="proj-1", run_id="run-1",
        graph_digest="g" * 8, plan_digest="p" * 8, policy_digest="y" * 8,
        run_state=run_state, receipt_sequence=7, receipt_head_hash="d" * 64,
        nodes=tuple(nodes), edges=tuple(edges), levels=tuple(tuple(lv) for lv in levels),
    )


def test_header_shows_identity_receipt_anchor_and_not_authority():
    md = render_state_markdown(_proj([_node("a", "SUCCEEDED")], levels=[["a"]], run_state="SUCCEEDED"))
    assert "# Run STATE — run-1" in md
    assert "not authority" in md
    assert "receipt #7" in md and "d" * 64 in md
    assert "org-1" in md and "proj-1" in md
    assert "**SUCCEEDED**" in md


def test_pending_interrupts_lists_awaiting_nodes():
    md = render_state_markdown(_proj([_node("gate", "AWAITING_APPROVAL")], levels=[["gate"]]))
    assert "## Pending interrupts" in md
    assert "**gate**" in md and "awaiting approval" in md


def test_pending_interrupts_none_when_no_awaiting():
    md = render_state_markdown(_proj([_node("a", "RUNNING")], levels=[["a"]]))
    assert "## Pending interrupts\n\nNone." in md


def test_waves_marks_the_current_wave():
    nodes = [_node("a", "SUCCEEDED"), _node("b", "RUNNING")]
    md = render_state_markdown(_proj(nodes, levels=[["a"], ["b"]]))
    assert "**Wave 0**" in md
    assert "**Wave 1** — **current**" in md  # wave 0 fully succeeded, wave 1 still running


def test_nodes_table_has_a_row_per_node():
    nodes = [_node("a", "SUCCEEDED"), _node("b", "PENDING")]
    md = render_state_markdown(_proj(nodes, levels=[["a", "b"]]))
    assert "| a | worker |" in md
    assert "| b | worker |" in md


def test_next_actions_reflects_run_state():
    done = render_state_markdown(_proj([_node("a", "SUCCEEDED")], levels=[["a"]], run_state="SUCCEEDED"))
    assert "Run complete" in done
    failed = render_state_markdown(_proj([_node("a", "FAILED")], levels=[["a"]], run_state="FAILED"))
    assert "Run failed at: a." in failed
    awaiting = render_state_markdown(_proj([_node("g", "AWAITING_APPROVAL")], levels=[["g"]]))
    assert "Waiting for approval on" in awaiting
    ready = render_state_markdown(_proj([_node("r", "READY")], levels=[["r"]]))
    assert "Ready to start: r" in ready


def test_is_deterministic():
    p = _proj([_node("a", "SUCCEEDED"), _node("b", "RUNNING")], levels=[["a"], ["b"]])
    assert render_state_markdown(p) == render_state_markdown(p)


def test_markdown_table_injection_is_escaped():
    # a node id containing the column separator must not break the table
    md = render_state_markdown(_proj([_node("a|b", "RUNNING")], levels=[["a|b"]]))
    assert "a\\|b" in md            # rendered escaped
    assert "| a|b | worker" not in md  # never a raw, column-breaking pipe in a cell
