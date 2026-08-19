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


def _mapping(value: object) -> dict:
    """A dict, or an empty one. `value or {}` is NOT this: a non-empty string is truthy and then
    `.get` raises. A receipt that crashes on a malformed ledger is a tool that fails at the exact
    moment its subject is suspect."""
    return value if isinstance(value, dict) else {}


def _attempts_consumed(entries: list) -> int:
    """Rows on which the worker was actually invoked.

    NOT ``len(entries)``, and not the last row's ``budget_spent["laps"]``. A ceiling halt records a
    final row on which no work was attempted, so counting rows reports 11 attempts against a
    declared 10 — a bound that held EXACTLY, reported as 1.1x over. `attempted` exists to make this
    computable; the receipt is the place that has to actually do it.
    """
    # Counts unless the flag is EXACTLY False. A missing, null or malformed value counts as an
    # attempt, deliberately: an unreadable flag that reduced reported consumption would make a
    # damaged or doctored ledger read as a cheaper run than it was. When the receipt cannot tell,
    # it must not flatter the run.
    return sum(1 for entry in entries if entry.get("attempted", True) is not False)


def _distinct_declarations(entries: list) -> list[dict]:
    """Every DISTINCT set of ceilings appearing in the ledger, in first-seen order.

    Usually one. `--resume` can produce more than one: a run halted at `--max-iterations 1` and
    resumed at `--max-iterations 5` leaves rows declaring both, and the two segments genuinely ran
    under different limits.
    """
    seen: list[dict] = []
    for entry in entries:
        declared = entry.get("budget_declared")
        if isinstance(declared, dict) and declared and declared not in seen:
            seen.append(declared)
    return seen


def _declared(entries: list) -> dict:
    """The ceilings this run ran under — ONLY if every row agrees. Otherwise empty.

    Refuses to pick. This function took the LAST row's declaration until an audit showed what that
    reports: a run halted at a declared ceiling of 1 attempt and then resumed at 5 produced
    "attempts 5/5", i.e. a run that blew a declared bound of 1 and went on to spend 5 attempts,
    rendered as a bound that held exactly. The earlier, tighter declaration was silently discarded —
    the one number an auditor asking "did this stay inside its budget?" most needs.

    Read from the LEDGER rather than metadata.json deliberately: `bl verify` hashes the ledger and
    does not hash metadata, so a ceiling quoted from metadata could be edited upward after the run
    and the receipt would still verify clean.
    """
    declarations = _distinct_declarations(entries)
    return declarations[0] if len(declarations) == 1 else {}


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
    spent = _mapping(entries[-1].get("budget_spent"))
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


#: A terminal ledger decision maps 1:1 onto the run's outcome. `continue` is not terminal — a
#: ledger whose last row says `continue` records a run that never finished, which is a fact about
#: the record and must be reported as one rather than smoothed into a status.
_DECISION_STATUS = {
    "done": "DONE", "halt": "HALT", "pause": "PAUSE", "killed": "KILLED", "error": "ERROR",
}


def _status_from_ledger(entries: list) -> str:
    """The run's outcome, derived from the hash-chained ledger rather than from metadata.json."""
    if not entries:
        return "NO LEDGER ROWS"
    decision = _mapping(entries[-1] if isinstance(entries[-1], dict) else {}).get("decision")
    if decision == "continue":
        return "INCOMPLETE"
    return _DECISION_STATUS.get(decision if isinstance(decision, str) else "", "UNKNOWN")


def receipt_document(metadata: dict, entries: list) -> dict:
    """The receipt as data. The single source both written artifacts render from.

    Pure: no clock, no filesystem, no re-running of anything. A receipt describes a run that already
    finished, so a function here that recomputed a verdict would be inventing one rather than
    reporting it.
    """
    declared = _declared(entries)
    declarations = _distinct_declarations(entries)
    gates = _distinct_gates(entries)
    spent = _mapping(entries[-1].get("budget_spent")) if entries else {}
    head = metadata.get("ledger_head") or ""
    # The run directory, taken from metadata's own ledger path rather than by touching the
    # filesystem, so this function stays pure. A literal "<run-dir>" placeholder produced a
    # copy-pasteable block that could not be pasted, which trains a reader to skip the instruction.
    ledger_path = metadata.get("ledger_path") or ""
    run_directory = str(Path(ledger_path).parent) if ledger_path else "<run-dir>"
    return {
        "run": {
            "id": metadata.get("run_id", ""),
            # From the LEDGER's terminal decision, which is inside the hash chain — not from
            # metadata.json, which `bl verify` reads and does NOT hash. Taking the headline from
            # metadata meant editing that one unhashed file flipped the receipt from HALT to DONE
            # while verification stayed green: the single most load-bearing word in the document
            # rested on the one file nothing protects.
            "status": _status_from_ledger(entries),
            "status_in_metadata": metadata.get("status", ""),
            "status_disagrees_with_metadata": (
                _status_from_ledger(entries) != (metadata.get("status") or "")
                and bool(metadata.get("status"))
            ),
            "reason": metadata.get("reason", ""),
            "laps": len(entries),
        },
        # Declared beside consumed, per dimension, so neither can be read without the other.
        # `changed_during_run` exists because refusing to pick a declaration must not read as
        # "no limits were declared". An absent number and a number that changed halfway are
        # different facts, and only one of them is a reason to distrust the summary.
        "bounds_recorded": bool(declarations),
        # Where WORK is cut off, as distinct from the declared total. None when not recorded.
        "work_ceiling_s": declared.get("wallclock_work_s"),
        "bounds_changed_during_run": len(declarations) > 1,
        "declarations": declarations if len(declarations) > 1 else [],
        "gate_changed_during_run": len(gates) > 1,
        "gates": gates if len(gates) > 1 else [],
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
                "passed": _mapping(entry.get("verdict")).get("passed") is True,
                "decision": entry.get("decision"),
                "attempted": bool(entry.get("attempted", True)),
                "detail": _mapping(entry.get("verdict")).get("detail", ""),
            }
            for entry in entries
        ],
        "integrity": {
            "authoritative_record": "ledger.jsonl",
            # Named for WHERE IT CAME FROM. The first version called this `ledger_head` and pasted
            # it straight into the verify command, which manufactured a false green: a complete
            # forgery of the run directory (ledger + metadata + this file) then passed
            # `bl verify --expect-head <that value>` and printed a success line claiming the digest
            # came from outside the directory. `bl verify --help` says supplying the head is "the
            # only check an adversary with write access to the whole run directory cannot satisfy" —
            # publishing the head INSIDE that directory hands the adversary exactly that check.
            # Reversing an earlier judgement here: a runnable command that proves nothing is worse
            # than a placeholder, because it converts a reader's diligence into false assurance.
            "ledger_head_in_this_directory": head,
            "verify_command": (
                f"bl verify {run_directory} --expect-head <the-digest-printed-when-the-run-ended>"
                if head else ""
            ),
            "note": (
                "This file is derived from ledger.jsonl and is NOT itself tamper-evident: it "
                "is written after the hash chain is closed and nothing hashes it. Neither is the "
                "digest recorded above — it sits in this directory, so anyone who could edit the "
                "ledger could edit it too. Supply the digest printed when the run ENDED, from your "
                "terminal, your CI log, or wherever you kept it. Without a digest held outside "
                "this directory, verification shows only that the file was not carelessly edited."
            ),
        },
    }


def _distinct_gates(entries: list) -> list[dict]:
    """Every DISTINCT gate appearing in the ledger, in first-seen order."""
    seen: list[dict] = []
    for entry in entries:
        gate = entry.get("gate")
        if isinstance(gate, dict) and gate.get("kind") and gate not in seen:
            seen.append(gate)
    return seen


def _gate_of(entries: list) -> dict:
    """The gate that decided the run — ONLY if one gate decided all of it. Otherwise empty.

    Same defect as `_declared`, same fix: a resumed run can change gate (`--gate-override` on the
    resume), and naming the last one attributes the whole run to a gate that decided only part of
    it. Naming no gate is worse reading and better evidence.
    """
    gates = _distinct_gates(entries)
    return gates[0] if len(gates) == 1 else {}


def _cell(pair: dict, unit: str = "", *, recorded: bool = True) -> tuple[str, str]:
    """One allowed/used pair.

    `recorded=False` renders "not recorded", which is NOT the same claim as "no ceiling". A ledger
    written before declared bounds existed carries no declaration; rendering that as "no ceiling"
    asserts the run was unbounded, which the data does not say and which is very likely false — the
    loop had a bounds.yaml, it simply was not written into the row. An absent record and a declared
    absence are different facts and the receipt must not merge them.
    """
    declared = pair.get("declared")
    consumed = pair.get("consumed")
    if not recorded:
        allowed = "not recorded"
    else:
        allowed = "no ceiling" if declared is None else f"{declared}{unit}"
    return (allowed, "?" if consumed is None else f"{consumed}{unit}")


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
        *(
            [
                "> **This run's own record disagrees with the summary filed beside it.** The status "
                f"above comes from the ledger (`{run['status']}`); `metadata.json` says "
                f"`{run['status_in_metadata']}`. The ledger is the hash-chained record; that file "
                "is not covered by it. Treat this run as suspect.",
                "",
            ]
            if run.get("status_disagrees_with_metadata") else []
        ),
        "## What this run was allowed, and what it used",
        "",
        "| | allowed | used |",
        "|---|---|---|",
    ]
    for label, key, unit in (
        ("attempts", "attempts", ""), ("tokens", "tokens", ""), ("wall clock", "wallclock_s", "s"),
    ):
        allowed, used = _cell(bounds[key], unit, recorded=document.get("bounds_recorded", True))
        if document.get("bounds_changed_during_run"):
            allowed = "**changed**"
        lines.append(f"| {label} | {allowed} | {used} |")

    work_ceiling = (document.get("work_ceiling_s")
                    if not document.get("bounds_changed_during_run") else None)
    if work_ceiling is not None and work_ceiling != bounds["wallclock_s"].get("declared"):
        # Both numbers are true and they answer different questions. The declared total is the
        # operator's; the work ceiling is where the run actually gets stopped, and quoting only the
        # total hides it.
        lines += [
            "",
            f"The wall-clock ceiling above is the total. Work stops at **{work_ceiling}s** — the "
            "remainder is held back so a run halted by a bound can still write its handoff, which "
            "is taken OUT of the declared ceiling rather than added to it.",
        ]

    if document.get("bounds_changed_during_run"):
        lines += [
            "",
            "> **The limits changed while this run was in progress**, so there is no single set to "
            "read the spend against. This happens when a run is resumed with different limits. "
            "The totals above are for the whole ledger; the segments ran under:",
            "",
        ]
        for index, declaration in enumerate(document.get("declarations") or [], start=1):
            def _shown(value: object, unit: str = "") -> str:
                return "no ceiling" if value is None else f"{value}{unit}"

            lines.append(
                f"> {index}. attempts {_shown(declaration.get('attempts'))}, "
                f"tokens {_shown(declaration.get('tokens'))}, "
                f"wall clock {_shown(declaration.get('wallclock_s'), 's')}"
            )

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
    elif document.get("gate_changed_during_run"):
        # Naming one of them would attribute the whole run to a gate that decided part of it.
        named = ", ".join(f"`{gate.get('kind')}`" for gate in document.get("gates") or [])
        lines.append(
            f"**More than one gate decided this run** — {named}. No single gate can be credited "
            "with the outcome; read the lap table."
        )
    else:
        # Absent rather than invented. A run recorded before provenance existed cannot be given a
        # gate name after the fact without the receipt asserting something nobody checked.
        lines.append("Not recorded — this run predates gate provenance in the ledger.")

    lines += ["", "## Laps", "", "| lap | verdict | decision | attempted | detail |", "|---|---|---|---|---|"]
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
