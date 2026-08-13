"""``bl graph resume`` — continue a run, and raise or lower its spend ceiling while doing it.

This exists because a budget pause is a DECISION POINT, not an ending. The run stops, the
operator is told what was spent and against which ceiling, and then they need exactly one
step to say "go on, with this much". Without such a step the pause is a dead end: the
controller refuses to continue a paused run with no ceiling declared (continuing with no limit
is not what a budget pause is asking for), and every other entry point passed none.

Lowering matters as much as raising. An operator watching a run burn faster than expected
should be able to bring the ceiling DOWN and have the next attempt refused, without editing a
manifest or hand-writing JSON.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from bounded_loops.graph.application.arena_projection import ArenaReadRequest
from bounded_loops.graph.application.budget_config import describe
from bounded_loops.graph.application.state_document import render_state_markdown
from bounded_loops.graph.domain.errors import GraphIntegrityError, GraphValidationError


def cmd_graph_resume(args: argparse.Namespace) -> int:
    """bl graph resume --run <dir> [--max-tokens N] [--max-cost-usd X] [--budget-file F]

    Continues an interrupted or budget-paused run. With no budget flags the run's own
    configuration applies; with them, they become this continuation's ceiling.
    """
    # Imported here, not at module scope, to keep `bl` startup off the heavy graph wiring —
    # the same pattern the other graph subcommands follow.
    from bounded_loops.graph.cli_graph import _err, _resolve_budget
    from bounded_loops.graph.cli_graph_approve import _load_identity_and_facade, _load_node_prompts
    from bounded_loops.graph.cli_graph_providers import _catalog_path

    run_dir = Path(args.run)
    if not run_dir.is_dir():
        _err(f"graph resume: '{run_dir}' is not a directory")
        return 2

    node_prompts, error = _load_node_prompts(getattr(args, "inputs", None))
    if error is not None:
        _err(f"graph resume: {error}")
        return 2

    try:
        run_budget, price_table = _resolve_budget(args)
    except GraphIntegrityError as exc:
        _err(f"graph resume: {exc}")
        return 2

    # The SAME catalog the run was created with. Without it, a run whose plan names a
    # catalog provider is unresumable — the wiring chokepoint refuses a provider this
    # process cannot run, which is right, and would be a trap without this flag.
    identity, facade = _load_identity_and_facade(
        run_dir, node_prompts or {}, _catalog_path(args),
    )
    if identity is None or facade is None:
        return 2

    request = ArenaReadRequest(
        subject_id=identity.organization_id,
        organization_id=identity.organization_id,
        project_id=identity.project_id,
        run_id=identity.run_id,
    )
    try:
        projection = facade.resume(
            request,
            run_budget=run_budget if run_budget.declared else None,
            price_table=price_table if price_table.prices else None,
        )
    except (GraphIntegrityError, GraphValidationError) as exc:
        # A run that paused on budget and is being continued with no ceiling lands here, with
        # the controller's own explanation. Surfaced verbatim rather than reworded: it already
        # names the flags to supply.
        _err(f"graph resume: {exc}")
        return 2

    if getattr(args, "json", False):
        print(json.dumps({
            "run": str(run_dir),
            "run_state": projection.run_state,
            "spend_tokens": projection.spend_tokens,
            "spend_cost_microunits": projection.spend_cost_microunits,
            "spend_complete": projection.spend_complete,
            "budget_pause": projection.budget_pause,
            "budget": describe(run_budget, price_table),
        }, indent=2, sort_keys=True))
        return 0

    print(render_state_markdown(projection))
    return 0


def add_resume_parser(graph_subs: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Register ``bl graph resume``. Shares the budget flags with ``bl graph run`` verbatim.

    Same flag names on both commands on purpose: an operator who learned ``--max-tokens`` when
    starting a run must not have to learn a second spelling to change it.
    """
    resume_p = graph_subs.add_parser(
        "resume",
        help="Continue an interrupted or budget-paused run, optionally with a new spend ceiling.",
        description=(
            "Continue a run. When it paused because its spend ceiling was reached, supply a new "
            "ceiling here to go on — or a lower one to stop sooner."
        ),
    )
    resume_p.add_argument("--run", required=True, metavar="<dir>",
                          help="The run directory reported by `bl graph run --execute`.")
    resume_p.add_argument("--inputs", default=None, metavar="<json>",
                          help=(
                              "Path to a JSON object mapping node_id -> prompt. Prompts are not "
                              "persisted, so re-supply them when resuming connector nodes."
                          ))
    resume_p.add_argument("--max-tokens", default=None, type=int, metavar="<n>",
                          help="New token ceiling for this continuation. Raise it to go past a "
                               "pause, or lower it to stop sooner.")
    resume_p.add_argument("--max-cost-usd", default=None, metavar="<amount>",
                          help="New cost ceiling for this continuation, in USD (e.g. 5.00). "
                               "Needs rates — see --budget-file.")
    resume_p.add_argument("--budget-file", default=None, metavar="<json>",
                          help="Budget file holding standing ceilings and the price table. "
                               "Explicit flags above override it per dimension.")
    resume_p.add_argument(
        "--providers", default=None, metavar="<catalog.toml>",
        help=(
            "Provider catalog (TOML) this run needs. Supply the SAME catalog the run was created with: a run whose plan names a catalog provider cannot be continued by a process that has never heard of it. BOUNDED_LOOPS_PROVIDERS is the machine-wide default."
        ),
    )
    resume_p.add_argument("--json", action="store_true", help="Emit JSON output.")
    resume_p.set_defaults(func=cmd_graph_resume)
