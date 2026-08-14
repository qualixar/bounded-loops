"""Render a run's STATE.md — the human-readable, per-run projection (ADR-12 D4).

STATE.md shows the SAME content the Arena renders (current wave, node states, pending
interrupts, gate outcomes, next actions), as Markdown. It is a READ-ONLY UX projection and
is NEVER authority: the append-only event log + receipts are the source of truth. This module
is a PURE function of an already-verified `ArenaProjection` (built by `read_arena_projection`
from a verified, authorized receipt snapshot) — it derives nothing from raw receipts itself,
so STATE.md can never become a second, divergent source of truth. The receipt sequence + head
hash are rendered as the anchor that ties this projection to the log it came from.
"""

from __future__ import annotations

from decimal import Decimal

from bounded_loops.graph.application.arena_projection import ArenaProjection
from bounded_loops.graph.domain.usage import MICROUNITS_PER_USD

_STATE_GLYPH = {
    "SUCCEEDED": "✓",
    "FAILED": "✗",
    "RUNNING": "▶",
    "GATING": "◌",
    "AWAITING_APPROVAL": "⏸",
    "READY": "○",
    "STARTING": "◐",
    "PENDING": "·",
}
# Order used for the one-line progress summary.
_PROGRESS_ORDER = (
    ("failed", "FAILED"),
    ("running", "RUNNING"),
    ("gating", "GATING"),
    ("awaiting approval", "AWAITING_APPROVAL"),
    ("starting", "STARTING"),
    ("ready", "READY"),
    ("pending", "PENDING"),
)


def render_state_markdown(projection: ArenaProjection) -> str:
    """Return the Markdown STATE.md for one verified Arena projection. Deterministic:
    the same projection always renders byte-identical output."""
    sections = (
        _header(projection),
        _progress(projection),
        _spend(projection),
        _pending_interrupts(projection),
        _waves(projection),
        _nodes_table(projection),
        _next_actions(projection),
    )
    return "\n\n".join(sections) + "\n"


def _header(p: ArenaProjection) -> str:
    return (
        f"# Run STATE — {_cell(p.run_id)}\n\n"
        "> Read-only projection regenerated from the event log — **not authority**. The event "
        f"log + receipts are the source of truth (reflects receipt #{p.receipt_sequence}, head "
        f"`{_code(p.receipt_head_hash)}`).\n\n"
        "| Field | Value |\n| --- | --- |\n"
        f"| Organization | {_cell(p.organization_id)} |\n"
        f"| Project | {_cell(p.project_id)} |\n"
        f"| Run | {_cell(p.run_id)} |\n"
        f"| Run state | **{_cell(p.run_state)}** |\n"
        f"| Graph digest | `{_code(p.graph_digest)}` |\n"
        f"| Plan digest | `{_code(p.plan_digest)}` |\n"
        f"| Policy digest | `{_code(p.policy_digest)}` |"
    )


def _progress(p: ArenaProjection) -> str:
    counts = _state_counts(p)
    total = len(p.nodes)
    parts = [f"**{counts.get('SUCCEEDED', 0)}/{total}** succeeded"]
    parts.extend(f"{counts[state]} {label}" for label, state in _PROGRESS_ORDER if counts.get(state))
    return "## Progress\n\n" + " · ".join(parts)


def _spend(p: ArenaProjection) -> str:
    """What this run has consumed, and whether the number is the truth or a floor.

    An under-count shown as a measurement is worse than showing nothing: an operator reading
    "1,200 tokens" makes different decisions than one reading "at least 1,200". So an
    incomplete total says so, in the same line, rather than in a footnote nobody reads.
    """
    if not p.spend_tokens and not p.spend_cost_microunits and p.spend_complete:
        return "## Spend\n\nNothing measured."
    qualifier = "" if p.spend_complete else " (at least — some attempts reported no usage)"
    cost = Decimal(p.spend_cost_microunits) / MICROUNITS_PER_USD
    return (
        "## Spend\n\n"
        f"**{p.spend_tokens:,}** tokens · **${cost:.6f}**{qualifier}"
    )


def _pending_interrupts(p: ArenaProjection) -> str:
    waiting = [node for node in p.nodes if node.state == "AWAITING_APPROVAL"]
    if not waiting:
        return "## Pending interrupts\n\nNone."
    lines = "\n".join(
        f"- **{_cell(node.node_id)}** (attempt {node.attempt}) is awaiting approval" for node in waiting
    )
    return "## Pending interrupts\n\n" + lines


def _waves(p: ArenaProjection) -> str:
    if not p.levels:
        return "## Waves\n\n_(no nodes)_"
    state_by_id = {node.node_id: node.state for node in p.nodes}
    current = _current_wave_index(p, state_by_id)
    out = ["## Waves"]
    for index, level in enumerate(p.levels):
        marker = " — **current**" if index == current else ""
        out.append(f"\n**Wave {index}**{marker}")
        out.extend(
            f"- {_glyph(state_by_id.get(node_id, 'PENDING'))} {_cell(node_id)} — {state_by_id.get(node_id, 'PENDING')}"
            for node_id in level
        )
    return "\n".join(out)


def _nodes_table(p: ArenaProjection) -> str:
    head = (
        "## Nodes\n\n"
        "| Node | Kind | State | Attempt | Effects | Isolation | Transport | Artifacts |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- |"
    )
    rows = [
        f"| {_cell(node.node_id)} | {_cell(node.kind)} | {_glyph(node.state)} {_cell(node.state)} "
        f"| {node.attempt} | {_cell(', '.join(node.required_effects) or '—')} | {_cell(node.isolation)} "
        f"| {_cell(node.transport or '—')} | {len(node.artifact_digests)} |"
        for node in p.nodes
    ]
    return head + "\n" + "\n".join(rows) if rows else head + "\n_(no nodes)_"


def _next_actions(p: ArenaProjection) -> str:
    if p.budget_pause is not None and p.run_state not in ("SUCCEEDED", "FAILED"):
        # Checked FIRST for a RUNNING run: without it a budget-paused run renders as "Running:
        # <node>", which is exactly wrong — nothing is running and nothing will until the
        # operator acts. Indistinguishable from progress is the failure mode a pause exists to
        # avoid.
        reason = p.budget_pause.get("reason", "the run budget was reached")
        return (
            "## Next actions\n\n"
            f"**Paused on budget** — {_cell(str(reason))}\n\n"
            "Continue with a higher ceiling:\n\n"
            "```\nbl graph resume --run <dir> --max-tokens <n>\n```\n\n"
            "Or `--max-cost-usd <amount>`, or `--budget-file <json>`. A LOWER ceiling stops "
            "the run sooner — the same command either way."
        )
    if p.run_state == "SUCCEEDED":
        return "## Next actions\n\nRun complete — all nodes succeeded."
    if p.run_state == "FAILED":
        failed = [node.node_id for node in p.nodes if node.state == "FAILED"]
        detail = f" at: {', '.join(_cell(node_id) for node_id in failed)}." if failed else "."
        return "## Next actions\n\nRun failed" + detail
    waiting = [node.node_id for node in p.nodes if node.state == "AWAITING_APPROVAL"]
    if waiting:
        return "## Next actions\n\nWaiting for approval on: " + ", ".join(f"**{_cell(x)}**" for x in waiting)
    running = [node.node_id for node in p.nodes if node.state == "RUNNING"]
    if running:
        return "## Next actions\n\nRunning: " + ", ".join(_cell(x) for x in running)
    ready = [node.node_id for node in p.nodes if node.state == "READY"]
    if ready:
        return "## Next actions\n\nReady to start: " + ", ".join(_cell(x) for x in ready)
    return f"## Next actions\n\nNo runnable nodes; run state is {_cell(p.run_state)}."


def _current_wave_index(p: ArenaProjection, state_by_id: dict[str, str]) -> int:
    # The current wave is the earliest level with a node not yet SUCCEEDED; -1 = all done.
    for index, level in enumerate(p.levels):
        if any(state_by_id.get(node_id) != "SUCCEEDED" for node_id in level):
            return index
    return -1


def _state_counts(p: ArenaProjection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for node in p.nodes:
        counts[node.state] = counts.get(node.state, 0) + 1
    return counts


def _glyph(state: str) -> str:
    return _STATE_GLYPH.get(state, "•")


def _cell(value: object) -> str:
    # Keep a dynamic value from breaking the Markdown table (| is the column separator) or
    # injecting layout (newlines). STATE.md is UX; render text safely and literally.
    text = str(value)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _code(value: object) -> str:
    # Inside a code span a backtick would end the span; digests are hex, but be defensive.
    return str(value).replace("`", "").replace("\n", " ").replace("\r", " ")
