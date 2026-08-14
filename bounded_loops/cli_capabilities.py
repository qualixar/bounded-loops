"""``bl capabilities`` — the same capability contract the MCP tool serves, on the command line.

Why both: a host that speaks MCP calls `bl_capabilities`, but plenty of hosts drive tools by
running a command and reading stdout, and a human debugging "why did it refuse my graph" should
not have to start an MCP server to find out. Both surfaces call `capability_report()`, so there
is exactly one capability document and no chance of the CLI and MCP answers disagreeing.
"""

from __future__ import annotations

import argparse
import json
from typing import Any, Mapping

from bounded_loops.graph.adapters.enforcement.snapshot import platform_snapshot
from bounded_loops.graph.application.capability_report import capability_report


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Register the ``capabilities`` subcommand onto *subparsers*."""
    parser = subparsers.add_parser(
        "capabilities",
        help="Print what this engine can actually do (and what it only declares).",
        description=(
            "The capability contract: node kinds, gate kinds and what each mechanically checks, "
            "isolation tiers and what each enforces on THIS host, which failure policies are "
            "honoured versus merely declared, the repair contract, effects, budget fields and "
            "where each is enforced, the terminal statuses, and every refusal with its fix. "
            "This is the document to read before authoring a graph."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the full document as JSON (what a tool should consume).",
    )
    parser.add_argument(
        "--refusals",
        action="store_true",
        help="Print only the refusal table: every rejection and how to fix it.",
    )
    parser.set_defaults(func=_cmd_capabilities)


def _cmd_capabilities(args: argparse.Namespace) -> int:
    report = capability_report(platform=platform_snapshot())

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    if args.refusals:
        _print_refusals(report["refusals"])
        return 0
    _print_summary(report)
    return 0


def _print_refusals(refusals: Mapping[str, Any]) -> None:
    print(f"{refusals['count']} refusals the compiler can raise\n")
    for entry in refusals["table"]:
        print(f"  {entry['code']}")
        print(f"    {entry['summary']}")
        print(f"    fix: {entry['fix']}")
        print()


def _print_summary(report: Mapping[str, Any]) -> None:
    engine = report["engine"]
    print(f"bounded-loops {engine['version']}  ({engine['graph_api_version']})")
    print()
    print(engine["what_it_is"])
    print()

    print("NODE KINDS")
    for entry in report["node_kinds"]:
        required = ", ".join(entry["extra_required_fields"]) or "—"
        print(f"  {entry['kind']:<16} requires: {required}")
    print()

    print("GATES  (a gate must be a different object from the worker, and mechanical)")
    for entry in report["gates"]["kinds"]:
        mark = "available" if entry["available_here"] else "NOT AVAILABLE HERE"
        print(f"  {entry['kind']:<20} {mark:<20} {entry['checks']}")
    print()

    print("ISOLATION  (on this host)")
    for tier in report["isolation"]["tiers"]:
        if tier["deliverable_here"]:
            controls = ", ".join(tier["controls_enforced_here"]) or "—"
            print(f"  {tier['level']:<28} enforces: {controls}")
        else:
            print(f"  {tier['level']:<28} REFUSED HERE — {tier['reason_if_not']}")
    never = report["isolation"]["never_available"]
    if never:
        print(f"  never available on any host: {', '.join(never)}")
    print()

    policies = report["failure_policies"]
    print("FAILURE POLICIES")
    print(f"  honoured: {', '.join(policies['honoured'])}")
    print(f"  REFUSED (declared by the schema, not routed by the runtime): "
          f"{', '.join(policies['refused'])}")
    print()

    statuses = report["terminal_statuses"]
    print("TERMINAL STATUSES")
    print(f"  success:     {', '.join(statuses['success'])}")
    print(f"  NOT success: {', '.join(statuses['not_success'])}")
    print()

    print(f"REFUSALS: {report['refusals']['count']} — see `bl capabilities --refusals`")
