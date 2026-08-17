"""
`bl prune` — the operator surface for receipt retention.

Split out of `cli.py` because that module is at its 800-line ceiling, and because
a command that deletes things is easier to review on its own.

The safety posture is deliberately more conservative than the rest of the CLI:

* **Dry run is the default.** `--yes` is required to delete. Printing a plan and
  stopping is the correct behaviour for a command whose mistake is unrecoverable.
* **No filter means no deletion.** `bl prune <dir>` with neither `--older-than`
  nor `--keep` reports and exits, rather than treating an absent filter as
  "everything".
* **Only terminal runs are eligible**, and the count of skipped in-flight runs is
  always printed, so a user who expected something gone learns why it stayed.

Row-level pruning is not offered. A ledger is a hash chain over its own lines, so
deleting a row produces a file that fails verification — converting an intact audit
record into what looks like evidence of tampering. `run_retention` states this at
length; the CLI simply has no flag for it.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from bounded_loops.application.run_retention import (
    collect_candidates,
    execute_prune,
    plan_prune,
)
from bounded_loops.domain.errors import ManifestError


def add_prune_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register `bl prune` as a top-level command.

    It reads more naturally as `bl runs prune`, and that is where it started. But
    `bl runs` already takes `loop-dir` as a positional, so argparse binds the word
    `prune` to that positional and then rejects the directory as an unknown
    subcommand — `bl runs prune loops/x` cannot parse. Caught by running it.

    Rather than contort the parser or demand `bl runs loops/x prune`, this is a
    top-level verb. That is arguably where a destructive command belongs anyway:
    visible in `bl --help` rather than nested behind a listing command.
    """
    prune = subparsers.add_parser(
        "prune",
        help="Delete old persisted runs for a loop. Dry run unless --yes is given.",
        description=(
            "Deletes whole persisted runs, never individual ledger rows: a ledger is a "
            "hash chain, so removing a row would make the remainder fail verification. "
            "Runs that have not reached a terminal status are never eligible."
        ),
    )
    prune.add_argument("loop_dir", metavar="loop-dir", type=Path)
    prune.add_argument(
        "--older-than",
        metavar="DAYS",
        type=float,
        default=None,
        help="Only prune runs whose newest file is older than DAYS.",
    )
    prune.add_argument(
        "--keep",
        metavar="N",
        type=int,
        default=None,
        help="Always keep the N most recent terminal runs.",
    )
    prune.add_argument(
        "--storage-root",
        metavar="DIR",
        type=Path,
        default=None,
        help="Controller storage root, if runs were written outside the loop package.",
    )
    prune.add_argument("--json", action="store_true", help="Emit JSON output.")
    prune.add_argument(
        "--yes",
        action="store_true",
        help="Actually delete. Without this the command only reports what it would do.",
    )
    prune.set_defaults(func=cmd_prune)


def cmd_prune(args: argparse.Namespace) -> int:
    loop_dir: Path = args.loop_dir.resolve()
    if not loop_dir.is_dir():
        print(f"bl prune: '{loop_dir}' is not a directory or does not exist.")
        return 2

    try:
        candidates = collect_candidates(loop_dir, storage_root=args.storage_root)
        plan = plan_prune(candidates, older_than_days=args.older_than, keep_last=args.keep)
    except (ManifestError, ValueError) as exc:
        print(f"bl prune: {exc}")
        return 2

    no_filter = args.older_than is None and args.keep is None
    removed: tuple[str, ...] = ()
    failures: tuple[tuple[str, str], ...] = ()

    if args.yes and plan.prune:
        removed, failures = execute_prune(
            plan, loop_dir=loop_dir, storage_root=args.storage_root
        )

    if args.json:
        print(
            json.dumps(
                {
                    "considered": plan.total_considered,
                    "planned": [c.run_id for c in plan.prune],
                    "kept_recent": [c.run_id for c in plan.kept_recent],
                    "kept_not_terminal": [c.run_id for c in plan.kept_not_terminal],
                    "removed": list(removed),
                    "failures": [{"run_id": r, "error": e} for r, e in failures],
                    "dry_run": not args.yes,
                    "no_filter": no_filter,
                }
            )
        )
    else:
        _print_human(plan, removed, failures, dry_run=not args.yes, no_filter=no_filter)

    return 1 if failures else 0


def _print_human(
    plan,
    removed: tuple[str, ...],
    failures: tuple[tuple[str, str], ...],
    *,
    dry_run: bool,
    no_filter: bool,
) -> None:
    print(f"Considered {plan.total_considered} persisted run(s).")

    if no_filter:
        print(
            "No retention filter given, so nothing is selected. "
            "Pass --older-than DAYS and/or --keep N."
        )
        return

    if plan.kept_not_terminal:
        # Always reported, never silent: a user who expected a run gone should be
        # told it is still running rather than left to guess.
        print(
            f"Skipped {len(plan.kept_not_terminal)} run(s) with no terminal status "
            f"(possibly still in flight): "
            + ", ".join(c.run_id for c in plan.kept_not_terminal)
        )

    if not plan.prune:
        print("Nothing to prune.")
        return

    verb = "Would delete" if dry_run else "Deleted"
    print(f"{verb} {len(plan.prune)} run(s):")
    for candidate in plan.prune:
        print(f"  {candidate.run_id}  status={candidate.status}  age={candidate.age_days:.1f}d")

    if dry_run:
        print("Dry run. Re-run with --yes to delete.")
    else:
        print(f"Removed {len(removed)} run director{'y' if len(removed) == 1 else 'ies'}.")

    for run_id, error in failures:
        print(f"  FAILED {run_id}: {error}")
