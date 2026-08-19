"""Rendering the human-readable receipt for one persisted run.

Extracted from ``cli`` when the per-lap view began carrying gate provenance. Not a cosmetic split:
the receipt is about to grow a written artifact and its own ``bl receipt`` command, and the argparse
wiring in ``cli`` is not where that belongs. A reader asking "what exactly does a receipt claim?"
should find one file, not a region of a command dispatcher.

Reads the run directory and prints. No I/O beyond stdout, no re-running of a gate — a receipt is a
record of what already happened, and anything here that recomputed a verdict would be inventing one.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Callable


def _attempts_consumed(entries: list) -> int:
    """Rows on which the worker was actually invoked.

    NOT ``len(entries)``, and not the last row's ``budget_spent["laps"]``. A ceiling halt records a
    final row on which no work was attempted, so counting rows reports 11 attempts against a
    declared 10 — a bound that held EXACTLY, reported as 1.1x over. `attempted` exists to make this
    computable; the receipt is the place that has to actually do it.
    """
    return sum(1 for entry in entries if entry.get("attempted", True))


def _declared(entries: list) -> dict:
    """The ceilings this run ran under, taken from the ledger rows themselves.

    Every row carries them and they are identical, so the last row is as good as the first. Read
    from the LEDGER rather than metadata.json deliberately: `bl verify` hashes the ledger and does
    not hash metadata, so a ceiling quoted from metadata could have been edited upward after the
    run and the receipt would still verify clean.
    """
    for entry in reversed(entries):
        declared = entry.get("budget_declared")
        if isinstance(declared, dict) and declared:
            return declared
    return {}


def _against(consumed: object, ceiling: object, unit: str = "") -> str:
    """One `consumed/ceiling` pair. An undeclared ceiling says so rather than printing nothing."""
    shown = "no ceiling" if ceiling is None else f"{ceiling}{unit}"
    return f"{consumed}{unit}/{shown}"


def _print_bounds_line(entries: list) -> None:
    """What the run was ALLOWED, beside what it used.

    The product is called bounded-loops and until this line existed its own receipt showed only
    consumption: `tokens=205` with nothing to read it against. A spend figure alone cannot support
    the claim the name makes.
    """
    declared = _declared(entries)
    if not declared or not entries:
        return   # a run recorded before this field existed prints exactly as it did before
    spent = entries[-1].get("budget_spent") or {}
    print(
        "bounds: "
        f"attempts {_against(_attempts_consumed(entries), declared.get('attempts'))}   "
        f"tokens {_against(spent.get('tokens', '?'), declared.get('tokens'))}   "
        f"wallclock {_against(spent.get('wallclock_s', '?'), declared.get('wallclock_s'), 's')}"
    )


def _print_run_receipt(receipt: dict) -> None:
    metadata = receipt["metadata"]
    run_id = metadata.get("run_id", "?")
    status = metadata.get("status", "UNKNOWN")
    reason = metadata.get("reason", "unknown")
    print(f"Run {run_id}: {status} ({reason})")
    print(f"Workspace: {metadata.get('workspace', 'unknown')}")
    print(f"Ledger: {metadata.get('ledger_path', 'unknown')}")
    print()
    _print_bounds_line(receipt["entries"])
    print()
    for entry in receipt["entries"]:
        verdict = entry.get("verdict", {})
        passed = verdict.get("passed") is True
        state = "PASS" if passed else "FAIL"
        budget = entry.get("budget_spent", {})
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


def receipt_document(metadata: dict, entries: list) -> dict:
    """The receipt as data. The single source both written artifacts render from.

    Pure: no clock, no filesystem, no re-running of anything. A receipt describes a run that already
    finished, so a function here that recomputed a verdict would be inventing one rather than
    reporting it.
    """
    declared = _declared(entries)
    spent = (entries[-1].get("budget_spent") or {}) if entries else {}
    head = metadata.get("ledger_head") or ""
    # The run directory, taken from metadata's own ledger path rather than by touching the
    # filesystem, so this function stays pure. A literal "<run-dir>" placeholder produced a
    # copy-pasteable block that could not be pasted, which trains a reader to skip the instruction.
    ledger_path = metadata.get("ledger_path") or ""
    run_directory = str(Path(ledger_path).parent) if ledger_path else "<run-dir>"
    return {
        "run": {
            "id": metadata.get("run_id", ""),
            "status": metadata.get("status", "UNKNOWN"),
            "reason": metadata.get("reason", ""),
            "laps": len(entries),
        },
        # Declared beside consumed, per dimension, so neither can be read without the other.
        "bounds": {
            "attempts": {
                "declared": declared.get("attempts"),
                "consumed": _attempts_consumed(entries),
            },
            "tokens": {"declared": declared.get("tokens"), "consumed": spent.get("tokens")},
            "wallclock_s": {
                "declared": declared.get("wallclock_s"), "consumed": spent.get("wallclock_s"),
            },
        },
        "gate": dict(_gate_of(entries)),
        "laps": [
            {
                "lap": entry.get("lap"),
                "passed": (entry.get("verdict") or {}).get("passed") is True,
                "decision": entry.get("decision"),
                "attempted": bool(entry.get("attempted", True)),
                "detail": (entry.get("verdict") or {}).get("detail", ""),
            }
            for entry in entries
        ],
        "integrity": {
            "authoritative_record": "ledger.jsonl",
            "ledger_head": head,
            "verify_command": (
                f"bl verify {run_directory} --expect-head {head}" if head else ""
            ),
            "note": (
                "This file is derived from ledger.jsonl and is NOT itself tamper-evident: it is "
                "written after the hash chain is closed and nothing hashes it. The ledger's chain "
                "is the record that cannot be edited without detection. Verify with the command "
                "above, supplying the head you recorded when the run ended."
            ),
        },
    }


def _gate_of(entries: list) -> dict:
    """The gate that decided the run, from the rows themselves."""
    for entry in reversed(entries):
        gate = entry.get("gate")
        if isinstance(gate, dict) and gate.get("kind"):
            return gate
    return {}


def _cell(pair: dict, unit: str = "") -> tuple[str, str]:
    declared = pair.get("declared")
    consumed = pair.get("consumed")
    return (
        "no ceiling" if declared is None else f"{declared}{unit}",
        "?" if consumed is None else f"{consumed}{unit}",
    )


def receipt_markdown(document: dict) -> str:
    """Render the document. Takes the DOCUMENT, not the raw entries, so the written Markdown and
    the written JSON cannot drift apart — there is one computation and two renderings of it."""
    run = document["run"]
    bounds = document["bounds"]
    gate = document.get("gate") or {}
    integrity = document["integrity"]

    lines = [
        f"# Run receipt — {run['id'] or '(unnamed run)'}",
        "",
        f"**{run['status']}** — {run['reason']}" if run["reason"] else f"**{run['status']}**",
        "",
        "## What this run was allowed, and what it used",
        "",
        "| | allowed | used |",
        "|---|---|---|",
    ]
    for label, key, unit in (
        ("attempts", "attempts", ""), ("tokens", "tokens", ""), ("wall clock", "wallclock_s", "s"),
    ):
        allowed, used = _cell(bounds[key], unit)
        lines.append(f"| {label} | {allowed} | {used} |")

    lines += ["", "## What decided it", ""]
    if gate.get("kind"):
        # The distribution clause appears only when there IS one. A shipped gate has no separate
        # distribution, and the first version rendered "shipped, from `shipped`" — a tautology in
        # the one sentence a reader consults to find out where their gate came from.
        distribution = gate.get("distribution")
        lines.append(
            f"Gate `{gate['kind']}` — {gate.get('source', 'unknown')}"
            + (f", from `{distribution}`" if distribution else "")
            + (f", implemented by `{gate['implementation']}`" if gate.get("implementation") else "")
        )
    else:
        # Absent rather than invented. A run recorded before provenance existed cannot be given a
        # gate name after the fact without the receipt asserting something nobody checked.
        lines.append("Not recorded — this run predates gate provenance in the ledger.")

    lines += ["", "## Laps", "", "| lap | gate | decision | attempted | detail |", "|---|---|---|---|---|"]
    for lap in document["laps"]:
        detail = str(lap.get("detail") or "").replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {lap['lap']} | {'PASS' if lap['passed'] else 'FAIL'} | {lap['decision']} "
            f"| {'yes' if lap['attempted'] else 'no'} | {detail} |"
        )

    lines += ["", "## Verifying this receipt", "", integrity["note"], ""]
    if integrity["verify_command"]:
        lines += ["```bash", integrity["verify_command"], "```", ""]
    return "\n".join(lines)


RECEIPT_FILES = ("receipt.md", "receipt.json")


def _write_atomically(path: Path, text: str) -> None:
    """Replace one file without a torn read, matching `run_store._write_json_atomically`.

    A half-written receipt is worse than none: it looks like a record and is not one.
    """
    handle, temp_name = tempfile.mkstemp(prefix=".receipt-", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def write_receipt_artifacts(run_dir: Path, metadata: dict, entries: list) -> list[Path]:
    """Write receipt.md and receipt.json into a run directory. Returns what it wrote."""
    document = receipt_document(metadata, entries)
    written = []
    for name, text in (
        ("receipt.json", json.dumps(document, indent=2, sort_keys=True) + "\n"),
        ("receipt.md", receipt_markdown(document)),
    ):
        target = run_dir / name
        _write_atomically(target, text)
        written.append(target)
    return written


def write_receipt_artifacts_or_warn(build: Callable[[], tuple[Path, dict, list]]) -> None:
    """Do the whole paperwork step, and NEVER fail a run because the paperwork failed.

    Called on the terminal path of a completed run. A read-only volume, a full disk or a permissions
    problem must not turn a run that reached DONE into a failure — the ledger is already written and
    already the authoritative record, so the artifact is a convenience on top of it. Warns on stderr
    so the absence is visible rather than silent.

    Takes a BUILDER rather than the finished inputs, so resolving the run directory and reading the
    ledger back are inside the guard too. The first version guarded only the write, and the read
    outside it raised `ManifestError` and broke a caller — the same "widened one operation and left
    its sibling exposed" mistake this codebase has made repeatedly. Everything that can fail while
    producing paperwork belongs on the same side of the try.
    """
    try:
        run_directory, metadata, entries = build()
        write_receipt_artifacts(run_directory, metadata, entries)
    except KeyboardInterrupt:
        raise
    except BaseException as exc:  # noqa: BLE001 — see docstring
        print(
            f"[bounded-loops] could not write the receipt artifact ({type(exc).__name__}: {exc}). "
            f"The ledger is unaffected and remains the authoritative record.",
            file=sys.stderr,
        )


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
