"""HTML rendering for `bl graph console` (Slice 3).

This is the ONLY module in the console package that builds HTML. Isolating it
here means the one place untrusted or echoed values (node ids from the plan,
the per-invocation token, a redirect notice built from a POST result) are
interpolated into markup is small and reviewable in one file. Every value is
passed through ``html.escape`` before it reaches the page — nothing here
trusts its inputs, even though today's callers (``server.py``) only ever pass
values that originated from the run's own compiled plan or from this same
process's own token.

Read-only: this module performs no I/O against a run directory and holds no
approval authority. It renders whatever ``ArenaProjection`` it is given.
"""

from __future__ import annotations

from decimal import Decimal
import html
from pathlib import Path

from bounded_loops.graph.domain.usage import MICROUNITS_PER_USD
from bounded_loops.graph.application.arena_projection import ArenaNodeProjection, ArenaProjection
from bounded_loops.graph.application.execute_graph import _awaiting_approval_nodes
from bounded_loops.graph.domain.events import GraphRunIdentity

_TEMPLATE_PATH = Path(__file__).with_name("console_template.html")
_BODY_MARKER = "<!--CONSOLE-BODY-->"


def load_template() -> str:
    """Return the static page shell, read fresh from disk on every call."""
    return _TEMPLATE_PATH.read_text(encoding="utf-8")


def render_console_page(
    *,
    identity: GraphRunIdentity,
    projection: ArenaProjection,
    token: str,
    notice: str | None,
    template: str | None = None,
) -> str:
    """Render the full console page for the current *projection*.

    ``template`` is injectable for tests; production callers always render the
    on-disk ``console_template.html`` (the default, ``template=None``).
    """
    document = template if template is not None else load_template()
    if _BODY_MARKER not in document:
        raise ValueError("console template is missing the CONSOLE-BODY marker")
    body = _render_body(identity=identity, projection=projection, token=token, notice=notice)
    return document.replace(_BODY_MARKER, body, 1)


def _render_body(
    *, identity: GraphRunIdentity, projection: ArenaProjection, token: str, notice: str | None,
) -> str:
    esc = html.escape
    parts: list[str] = [_render_run_summary(identity, projection)]
    if notice:
        parts.append(f'<div class="notice">{esc(notice)}</div>')

    parts.append(_render_spend(projection))
    if projection.budget_pause is not None and projection.run_state not in ("SUCCEEDED", "FAILED"):
        parts.append(_render_budget_pause(projection, token=token))

    awaiting = _awaiting_approval_nodes(projection)
    if not awaiting:
        if projection.budget_pause is None or projection.run_state in ("SUCCEEDED", "FAILED"):
            parts.append('<p class="empty">Nothing is currently awaiting approval.</p>')
        return "\n".join(parts)

    nodes_by_id = {node.node_id: node for node in projection.nodes}
    for node_id in awaiting:
        parts.append(_render_node_card(nodes_by_id[node_id], token=token))
    return "\n".join(parts)


def _render_spend(projection: ArenaProjection) -> str:
    """What the run has consumed, in words a non-technical reader can act on.

    An incomplete total says "at least", never a bare figure: "1,200 tokens" and "at least
    1,200 tokens" support different decisions, and only one of them is true when some attempt
    reported nothing.
    """
    esc = html.escape
    if not projection.spend_tokens and not projection.spend_cost_microunits:
        return '<p class="spend">No spend measured yet.</p>'
    cost = Decimal(projection.spend_cost_microunits) / MICROUNITS_PER_USD
    prefix = "Spent" if projection.spend_complete else "Spent at least"
    return (
        f'<p class="spend">{esc(prefix)} '
        f"<strong>{projection.spend_tokens:,}</strong> tokens "
        f"(<strong>${cost:.6f}</strong>)</p>"
    )


def _render_budget_pause(projection: ArenaProjection, *, token: str) -> str:
    """The stop-and-decide card: what the ceiling was, and a box to set a new one.

    This is the whole point of pausing rather than failing. A non-technical operator should not
    have to learn a CLI flag, edit a manifest, or hand-write JSON to say "yes, spend a bit
    more" — or "actually, stop sooner". One number, one button, either direction.
    """
    esc = html.escape
    pause = projection.budget_pause or {}
    reason = str(pause.get("reason", "the spend ceiling for this run was reached"))
    token_attr = esc(token, quote=True)
    ceiling = pause.get("max_tokens")
    cost_ceiling = pause.get("max_cost_microunits")
    limits: list[str] = []
    if isinstance(ceiling, int):
        limits.append(f"token ceiling <strong>{ceiling:,}</strong>")
    if isinstance(cost_ceiling, int):
        as_usd = Decimal(cost_ceiling) / MICROUNITS_PER_USD
        limits.append(f"cost ceiling <strong>${as_usd:.6f}</strong>")
    return (
        '<div class="card">'
        "<h2>Paused — spending limit reached</h2>"
        f"<p>{esc(reason)}</p>"
        + (f"<p>Authorised: {' · '.join(limits)}</p>" if limits else "")
        + '<form method="post" action="/continue">'
        f'<input type="hidden" name="token" value="{token_attr}">'
        '<label>New token ceiling'
        ' <input type="number" name="max_tokens" min="1" step="1" placeholder="e.g. 500000">'
        "</label>"
        '<label>or new cost ceiling in USD'
        ' <input type="text" name="max_cost_usd" placeholder="e.g. 5.00">'
        "</label>"
        '<button type="submit" class="approve">Continue with this limit</button>'
        "</form>"
        "<p><small>A LOWER number stops the run sooner. Nothing continues until you choose."
        "</small></p>"
        "</div>"
    )


def _render_run_summary(identity: GraphRunIdentity, projection: ArenaProjection) -> str:
    esc = html.escape
    return (
        '<p class="run">run '
        f"<code>{esc(identity.organization_id)}/{esc(identity.project_id)}/{esc(identity.run_id)}</code>"
        f" — state <strong>{esc(projection.run_state)}</strong></p>"
    )


def _render_node_card(node: ArenaNodeProjection, *, token: str) -> str:
    esc = html.escape
    effects = ", ".join(sorted(node.required_effects)) or "none"
    node_id = esc(node.node_id)
    token_attr = esc(token, quote=True)
    node_id_attr = esc(node.node_id, quote=True)
    return (
        '<div class="card">'
        f"<h2>{node_id}</h2>"
        "<dl>"
        f"<dt>kind</dt><dd>{esc(node.kind)}</dd>"
        f"<dt>effects</dt><dd>{esc(effects)}</dd>"
        f"<dt>isolation</dt><dd>{esc(node.isolation)}</dd>"
        "</dl>"
        '<form method="post" action="/approve">'
        f'<input type="hidden" name="token" value="{token_attr}">'
        f'<input type="hidden" name="node_id" value="{node_id_attr}">'
        '<button type="submit" class="approve">Approve</button>'
        "</form>"
        '<form method="post" action="/reject">'
        f'<input type="hidden" name="token" value="{token_attr}">'
        f'<input type="hidden" name="node_id" value="{node_id_attr}">'
        '<button type="submit" class="reject">Reject</button>'
        "</form>"
        "</div>"
    )
