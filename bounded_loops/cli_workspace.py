"""``bl init`` and ``bl where`` — make the project home visible and creatable.

``bl where`` exists because a resolver that cannot explain itself is unusable: when receipts
land somewhere unexpected, the first question is "which workspace did you pick, and why", and
the answer must be one command away rather than a source-reading exercise.

Neither command touches a run directory. ``bl where`` creates nothing at all.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bounded_loops.domain.errors import ManifestError
from bounded_loops.workspace import Workspace, discover, ensure


def _err(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Register the ``init`` and ``where`` subcommands onto *subparsers*."""
    init_parser = subparsers.add_parser(
        "init",
        help="Create this project's .bounded-loops/ workspace.",
        description=(
            "Creates .bounded-loops/ — the project home for graphs, loop packages, run "
            "receipts, and tickets. Placed at the git repository root when there is one, "
            "otherwise in the given directory. Idempotent: run it as often as you like, and "
            "an existing config.toml is never overwritten."
        ),
    )
    init_parser.add_argument(
        "directory",
        nargs="?",
        type=Path,
        default=None,
        metavar="<dir>",
        help="Where to start looking (default: the current directory).",
    )
    init_parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        metavar="<dir>",
        help="Put the workspace under this directory instead of resolving one.",
    )
    init_parser.add_argument("--json", action="store_true", help="Emit JSON.")
    init_parser.set_defaults(func=_cmd_init)

    where_parser = subparsers.add_parser(
        "where",
        help="Print the resolved .bounded-loops/ workspace and why it was chosen.",
        description=(
            "Resolves this project's workspace and explains the choice. Creates nothing, so "
            "it is safe to run anywhere. Exit code is always 0 — not having a workspace yet "
            "is a valid answer, reported as \"exists\": false."
        ),
    )
    where_parser.add_argument(
        "directory",
        nargs="?",
        type=Path,
        default=None,
        metavar="<dir>",
        help="Where to start looking (default: the current directory).",
    )
    where_parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        metavar="<dir>",
        help="Resolve as if this directory held the workspace.",
    )
    where_parser.add_argument("--json", action="store_true", help="Emit JSON.")
    where_parser.set_defaults(func=_cmd_where)


# ── bl init ──────────────────────────────────────────────────────────────────


def _cmd_init(args: argparse.Namespace) -> int:
    try:
        workspace = discover(start=args.directory, explicit=args.workspace)
        created = ensure(workspace)
    except ManifestError as exc:
        _err(f"bl init: {exc}")
        return 2

    if args.json:
        print(
            json.dumps(
                {
                    "root": str(workspace.root),
                    "project_root": str(workspace.project_root),
                    "origin": workspace.origin,
                    "reason": workspace.reason,
                    "created": [str(path) for path in created],
                },
                indent=2,
            )
        )
        return 0

    print(f"workspace: {workspace.root}")
    print(f"  chosen because {workspace.reason}")
    if created:
        print("  created:")
        for path in created:
            print(f"    {path}")
    else:
        print("  already complete — nothing to create")
    print()
    print("Next: `bl where` to confirm, or `bl graph run <manifest> --execute` to fill runs/.")
    return 0


# ── bl where ─────────────────────────────────────────────────────────────────


def _cmd_where(args: argparse.Namespace) -> int:
    try:
        workspace = discover(start=args.directory, explicit=args.workspace)
    except ManifestError as exc:
        _err(f"bl where: {exc}")
        return 2

    counts = _counts(workspace)

    if args.json:
        print(
            json.dumps(
                {
                    "root": str(workspace.root),
                    "project_root": str(workspace.project_root),
                    "origin": workspace.origin,
                    "reason": workspace.reason,
                    "exists": workspace.exists(),
                    "counts": counts,
                },
                indent=2,
            )
        )
        return 0

    print(f"workspace: {workspace.root}")
    print(f"  chosen because {workspace.reason}")
    if not workspace.exists():
        print("  does not exist yet — run `bl init` to create it")
        return 0
    print(
        "  contents: "
        f"{counts['runs']} run(s), {counts['graphs']} graph(s), "
        f"{counts['loops']} loop package(s), {counts['tickets']} ticket(s)"
    )
    return 0


def _counts(workspace: Workspace) -> dict[str, int]:
    """Cheap directory counts. Deliberately not read from index.json, which is only a cache."""
    return {
        "runs": _count(workspace.runs_dir, directories=True),
        "graphs": _count(workspace.graphs_dir, directories=False),
        "loops": _count(workspace.loops_dir, directories=True),
        "tickets": _count(workspace.tickets_dir, directories=False),
    }


def _count(directory: Path, *, directories: bool) -> int:
    if not directory.is_dir():
        return 0
    try:
        entries = list(directory.iterdir())
    except OSError:
        return 0
    return sum(1 for entry in entries if entry.is_dir() == directories)
