"""Printing a run receipt to a terminal, and the `bl receipt` subcommand.

The receipt's CONTENT lives in `bounded_loops.application.receipt` so that every path which persists
a run can write it. This module is the presentation half: stdout and argparse only.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bounded_loops.application.receipt import (
    RECEIPT_FILES,
    _attempts_consumed,
    _declared,
    _mapping,
    _status_from_ledger,
    _against,
    _spend_across_segments,
    receipt_document,
    receipt_markdown,
    write_receipt_artifacts,
)


def _print_bounds_line(entries: list) -> None:
    """What the run was ALLOWED, beside what it used.

    The product is called bounded-loops and until this line existed its own receipt showed only
    consumption: `tokens=205` with nothing to read it against. A spend figure alone cannot support
    the claim the name makes.
    """
    declared = _declared(entries)
    if not declared or not entries:
        return   # a run recorded before this field existed prints exactly as it did before
    spent = _spend_across_segments(entries)
    print(
        "bounds: "
        f"attempts {_against(_attempts_consumed(entries), declared.get('attempts'))}   "
        f"tokens {_against(spent.get('tokens', '?'), declared.get('tokens'))}   "
        f"wallclock {_against(spent.get('wallclock_s', '?'), declared.get('wallclock_s'), 's')}"
    )


def _print_run_receipt(receipt: dict) -> None:
    metadata = receipt["metadata"]
    run_id = metadata.get("run_id", "?")
    # From the ledger, like the reason below and like the portable receipt. This surface still
    # headlined the unprotected summary file after the portable artifact stopped doing so — the
    # sibling of a fix already made, missed once again.
    status = _status_from_ledger(receipt["entries"]) or metadata.get("status", "UNKNOWN")
    # From the ledger, like the portable receipt. An audit noted this surface still printed
    # metadata.json's copy, which `bl verify` reads and does NOT hash — so the one reader who runs
    # `bl runs --show` instead of opening receipt.md saw the unprotected string. Two surfaces
    # describing the same run must not draw it from different files.
    entries = receipt["entries"]
    ledger_reason = ""
    if entries:
        last = entries[-1] if isinstance(entries[-1], dict) else {}
        detail = _mapping(last.get("verdict")).get("detail")
        ledger_reason = detail if isinstance(detail, str) else ""
    reason = ledger_reason or metadata.get("reason", "unknown")
    print(f"Run {run_id}: {status} ({reason})")
    print(f"Workspace: {metadata.get('workspace', 'unknown')}")
    print(f"Ledger: {metadata.get('ledger_path', 'unknown')}")
    print()
    _print_bounds_line(receipt["entries"])
    print()
    for entry in receipt["entries"]:
        verdict = _mapping(entry.get("verdict"))
        passed = verdict.get("passed") is True
        state = "PASS" if passed else "FAIL"
        budget = _mapping(entry.get("budget_spent"))
        print(
            f"Lap {entry.get('lap', '?')}: {state}  "
            f"decision={entry.get('decision', '?')}  "
            f"tokens={budget.get('tokens', '?')}  "
            f"wallclock={budget.get('wallclock_s', '?')}s"
        )
        detail = verdict.get("detail")
        if detail:
            print(f"  {detail}")
        # WHICH gate decided this lap. Printed because a verdict without its source is not
        # reviewable, and because provenance recorded in the ledger and shown nowhere is an
        # orphaned capability — the defect class this project keeps catching in itself. Absent for
        # runs written before the field existed, which print exactly as they did before.
        gate = entry.get("gate") or {}
        if isinstance(gate, dict) and gate.get("kind"):
            source = gate.get("source", "unknown")
            origin = gate.get("distribution") or source
            print(
                f"  gate: {gate['kind']} ({source}"
                + (f", {origin}" if origin != source else "")
                + (f") via {gate['implementation']}" if gate.get("implementation") else ")")
            )


# ── the portable artifact ──────────────────────────────────────────────────────────────────
#
# `bl runs --show` prints to a terminal. That cannot be attached to a paper, a pull request or a
# compliance ticket, which is where a receipt is actually asked for. These write a file.
#
# The artifact is DERIVED from the ledger and is NOT itself tamper-evident: it is written after the
# hash chain is closed and nothing hashes it. Saying so inside the file is the whole difference
# between a receipt and a decoration — it carries the ledger head and the command that checks it, so
# a reader who does not trust the file has a way to find out rather than a reassurance.


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "receipt",
        help="Render a run's receipt, or write it as a portable file.",
        description=(
            "Reads a run directory and renders its receipt: what the run was allowed, what it "
            "used, which gate decided it, and how to verify the record. Reads only; never re-runs "
            "a gate. The written artifact is DERIVED from ledger.jsonl and is not itself "
            "tamper-evident, which it says on its face along with the command that checks it."
        ),
    )
    parser.add_argument(
        "target", type=Path,
        help="A run directory containing ledger.jsonl and metadata.json.",
    )
    parser.add_argument("--json", action="store_true", help="Emit the receipt as JSON.")
    parser.add_argument(
        "--write", action="store_true",
        help=f"Write {' and '.join(RECEIPT_FILES)} into the run directory.",
    )
    parser.set_defaults(func=_cmd_receipt)


def _load_run(target: Path) -> tuple[dict, list] | None:
    """Metadata and ledger rows from a run directory, or None if it is not one."""
    ledger = target / "ledger.jsonl"
    metadata_path = target / "metadata.json"
    if not ledger.is_file():
        return None
    entries = []
    for line in ledger.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                return None
    metadata: dict = {}
    if metadata_path.is_file() and not metadata_path.is_symlink():
        try:
            loaded = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded = None
        if isinstance(loaded, dict):
            metadata = loaded
    return metadata, entries


def _cmd_receipt(args: argparse.Namespace) -> int:
    loaded = _load_run(args.target)
    if loaded is None:
        print(
            f"receipt: {args.target} is not a readable run directory "
            "(expected ledger.jsonl inside it)",
            file=sys.stderr,
        )
        return 2
    metadata, entries = loaded
    document = receipt_document(metadata, entries)

    if args.write:
        for path in write_receipt_artifacts(args.target, metadata, entries):
            print(f"wrote {path}")
        return 0
    if args.json:
        print(json.dumps(document, indent=2, sort_keys=True))
        return 0
    print(receipt_markdown(document))
    return 0
