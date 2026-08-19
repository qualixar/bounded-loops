"""What a run receipt CLAIMS, as data — and writing it into a run directory.

Lives in `application` rather than beside the CLI because `run_store.write_run_metadata` has to be
able to call it. Three separate code paths persist a run (the CLI, the MCP server, and the graph
loop bridge) and only the CLI wrote a receipt, so the other two left a STALE receipt sitting beside a
newer ledger while `bl verify` reported everything intact. Patching the two call sites would have
repeated this codebase's most-committed defect; putting the write where the metadata write already
lives makes the omission unrepresentable instead.

Everything here is a total function of its arguments except the two writers at the bottom. No clock,
no gate re-run: a receipt describes a run that already finished, so anything here that recomputed a
verdict would be inventing one rather than reporting it.
"""
from __future__ import annotations

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
    absent = False
    for entry in entries:
        declared = entry.get("budget_declared")
        if isinstance(declared, dict) and declared:
            if declared not in seen:
                seen.append(declared)
        else:
            absent = True
    # A row with NO declaration is a DIFFERENT state, not a row to skip. Skipping it meant a ledger
    # whose early rows predate the field and whose later rows carry it read as UNIFORM, and the whole
    # run was then reported as having run under the later declaration — including the segment that
    # never declared anything. Returning both states makes the caller treat it as changed.
    if absent and seen:
        return [*seen, {}]
    return seen


def _segments(entries: list) -> list[list]:
    """Split the ledger where a lap number stops increasing — i.e. at each `--resume`.

    `--resume` builds a FRESH `BudgetMeter` and restarts the lap counter at 1, while the ledger stays
    append-only. So a resumed run's rows are several independent meters concatenated, and the lap
    number is the only thing in the row that marks the seam.
    """
    segments: list[list] = []
    previous = None
    for entry in entries:
        lap = entry.get("lap") if isinstance(entry, dict) else None
        starts_new = (
            not segments
            or not isinstance(lap, int)
            or not isinstance(previous, int)
            or lap <= previous
        )
        if starts_new:
            segments.append([entry])
        else:
            segments[-1].append(entry)
        previous = lap
    return segments


def _last_value(segment: list, key: str) -> object:
    """The most recent numeric value for one budget dimension within a segment.

    Searched independently per dimension, because a row can carry one figure and not another. Within
    a segment `budget_spent` accumulates, so the LAST row that reports this key is the segment total
    for it.
    """
    for row in reversed(segment):
        value = _mapping(row.get("budget_spent") if isinstance(row, dict) else {}).get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
    return None


def _spend_across_segments(entries: list) -> dict:
    """Total spend for the RUN, not for its final segment.

    `budget_spent` is CUMULATIVE within one invocation's meter and resets on resume, so the last
    row's figures describe only the last segment. Reading them as the run's total under-reported a
    resumed run by every earlier segment — while `attempts` was already counted across the whole
    ledger, so one table held two different intervals. Both auditors called this a blocker, and they
    were right: a cost figure that shrinks when a run is resumed is worse than no cost figure.

    Sums each segment's LAST row, because within a segment the figures already accumulate.
    """
    total_tokens = 0.0
    total_wallclock = 0.0
    saw_tokens = False
    saw_wallclock = False
    for segment in _segments(entries):
        # PER DIMENSION, and that distinction is the whole point. Taking the last row that carried
        # ANY figures was the previous fix, and an audit showed it under-reports: a later row holding
        # only `{"laps": 2}` is non-empty, so it won the search and reported tokens as unknown while
        # an earlier row in the same segment recorded 205. A killed run's final row is a pre-turn
        # check written with `budget_spent={}`, and a bound halt writes a wind-down row, so "the last
        # row" is the wrong answer for either dimension — but so is "the last row with anything".
        # Under-reporting cost is the direction a receipt must never fail in.
        last = {
            key: _last_value(segment, key) for key in ("tokens", "wallclock_s")
        }
        tokens = last.get("tokens")
        wallclock = last.get("wallclock_s")
        if isinstance(tokens, (int, float)) and not isinstance(tokens, bool):
            total_tokens += tokens
            saw_tokens = True
        if isinstance(wallclock, (int, float)) and not isinstance(wallclock, bool):
            total_wallclock += wallclock
            saw_wallclock = True
    return {
        "tokens": (int(total_tokens) if total_tokens.is_integer() else total_tokens)
        if saw_tokens else None,
        "wallclock_s": round(total_wallclock, 2) if saw_wallclock else None,
        "segments": len(_segments(entries)) if entries else 0,
    }


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


#: A terminal ledger decision maps 1:1 onto the run's outcome. `continue` is not terminal — a
#: ledger whose last row says `continue` records a run that never finished, which is a fact about
#: the record and must be reported as one rather than smoothed into a status.
_DECISION_STATUS = {
    "done": "DONE", "halt": "HALT", "pause": "PAUSE", "killed": "KILLED", "error": "ERROR",
}


def _reason_from_ledger(entries: list) -> str:
    """The terminal row's own explanation, which is inside the hash chain.

    `metadata.json` stores the same string, and the STATUS was moved off that file already — but the
    reason was left behind, so half the receipt's headline still rested on the one file `bl verify`
    reads and does not hash. "HALT — max_iterations 2 reached at lap 3" could keep its verified HALT
    and have the clause after the dash rewritten to anything. Fixing a defect at one site and
    leaving its sibling is the mistake this codebase has made most often; this is that mistake, in
    the fix for it.
    """
    if not entries:
        return ""
    last = _mapping(entries[-1] if isinstance(entries[-1], dict) else {})
    decision = last.get("decision")
    # A paused run's reason is STRUCTURAL, not the gate's sentence. The gate genuinely passed and the
    # run then stopped for approval, so `**PAUSE** — gate passed (exit 0)` put a true clause where a
    # reader reads "why", and the actual why — awaiting approval — appeared nowhere. The label comes
    # from the hashed `decision`, so this adds no dependency on the unprotected summary file.
    if decision == "pause":
        return "awaiting-approval"
    detail = _mapping(last.get("verdict")).get("detail")
    return detail if isinstance(detail, str) else ""


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
    spent = _spend_across_segments(entries) if entries else {}
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
            # STATUS only. The reason strings are NOT comparable and must never be compared here:
            # the engine's `Outcome.reason` is a canonical label ("gate-passed", "awaiting-approval")
            # while the ledger's terminal detail is the GATE's own sentence ("gate passed (exit 0)").
            # For DONE and PAUSE they always differ, so folding reason into this flag made EVERY
            # honest successful run print "Treat this run as suspect" — and print it incoherently,
            # since the banner shows the status pair, which was identical. A receipt that accuses
            # itself on a clean run is worse than the defect the flag was added to catch.
            #
            # Nothing is lost by dropping it: the receipt DISPLAYS the ledger's reason, so metadata's
            # copy is not load-bearing and editing it changes nothing a reader sees.
            "status_disagrees_with_metadata": (
                _status_from_ledger(entries) != (metadata.get("status") or "")
                and bool(metadata.get("status"))
            ),
            # From the ledger, for the same reason as `status` above.
            "reason": _reason_from_ledger(entries),
            "reason_in_metadata": metadata.get("reason", ""),
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
        # How many independent meters this ledger holds. >1 means the run was resumed, which is why
        # the spend figures are a SUM rather than the last row's.
        "segments": spent.get("segments", 1) if entries else 0,
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
                # The SAME rule the counter uses (`is not False`). These disagreed: the summary
                # counted a malformed flag as an attempt while this table printed "no" for it, so one
                # document rendered one field two ways. A receipt that contradicts itself is not
                # evidence, whichever half happens to be right.
                "attempted": entry.get("attempted", True) is not False,
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
                "this directory, verification shows only that the LEDGER was not carelessly "
                "edited. It does not read this file at all."
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


def _remove_quietly(path: Path) -> bool:
    """Delete if present. Returns whether the path is now GONE.

    Returns a bool because the caller reports to a human: swallowing the failure and then printing
    "removed rather than left stale" made the message a claim the code had not earned — an audit
    called it best-effort that then lies that it succeeded. A stale receipt surviving a failed unlink
    is exactly the case the message must not paper over.
    """
    try:
        path.unlink()
    except FileNotFoundError:
        return True
    except (OSError, ValueError):
        return not path.exists()
    return True


def write_receipt_artifacts(run_dir: Path, metadata: dict, entries: list) -> list[Path]:
    """Write receipt.md and receipt.json into a run directory. Returns what it wrote.

    ABSENCE IS HONEST; STALENESS IS A LIE. Two files cannot be replaced in one atomic step, so on any
    failure this DELETES both rather than leaving whatever landed. An audit escalated the alternative:
    a crash between the two replaces left a new receipt.json beside an OLD receipt.md, and a paper
    attaches the markdown — so the reader gets a confident document describing a different run, with
    `bl verify` green because it never reads either file. A missing receipt sends someone to
    `bl receipt`, which re-derives the truth from the ledger. A stale one does not.
    """
    document = receipt_document(metadata, entries)
    # Both texts rendered BEFORE any write, so a rendering failure cannot half-write the pair.
    rendered = (
        ("receipt.json", json.dumps(document, indent=2, sort_keys=True) + "\n"),
        ("receipt.md", receipt_markdown(document)),
    )
    written: list[Path] = []
    try:
        for name, text in rendered:
            target = run_dir / name
            _write_atomically(target, text)
            written.append(target)
    except BaseException:
        # Only when a PARTIAL pair exists. An audit caught the previous version destroying a correct
        # receipt: `bl receipt --write` on an unchanged ledger, failing on the FIRST write, deleted a
        # pair that was accurate and consistent. Nothing had been overwritten at that point, so there
        # was nothing inconsistent to clean up — "absence is honest" is right for a STALE artifact and
        # wrong for a regeneration that simply could not run. Destroying a true document is a defect
        # this code did not have before the fix that introduced it.
        #
        # A partial pair is different: one new file beside one old one is inconsistent by
        # construction, and a paper attaches the markdown. That gets removed.
        if written:
            for name, _ in rendered:
                _remove_quietly(run_dir / name)
        raise
    return written


def write_receipt_artifacts_or_warn(
    run_dir: Path, build: Callable[[], tuple[dict, list]],
) -> None:
    """Do the whole paperwork step, and NEVER fail a run because the paperwork failed.

    Called on the terminal path of a completed run. A read-only volume, a full disk or a permissions
    problem must not turn a run that reached DONE into a failure — the ledger is already written and
    already the authoritative record, so the artifact is a convenience on top of it.

    `run_dir` is a SEPARATE argument from `build` on purpose. It used to come back from the builder,
    which meant a failure inside the builder left the directory unknown and any previous receipt
    sitting there untouched — a document describing an earlier state of this very run. Taking the
    directory up front makes cleanup always possible, which is the whole point: ABSENCE IS HONEST,
    STALENESS IS A LIE. A missing receipt sends a reader to `bl receipt`, which re-derives the truth
    from the ledger; a stale one sends them nowhere.
    """
    try:
        metadata, entries = build()
        write_receipt_artifacts(run_dir, metadata, entries)
    except KeyboardInterrupt:
        raise
    except BaseException as exc:  # noqa: BLE001 — see docstring
        # This path is the TERMINAL path of a run that has just been persisted, so any receipt already
        # sitting here describes an EARLIER state of this same run — for a resumed run, only its first
        # segment. That is stale by construction, so it goes regardless of how far the write got. The
        # narrower rule in `write_receipt_artifacts` protects explicit regeneration; this one protects
        # a reader from a confident document about a run that has since moved on.
        survivors = [name for name in RECEIPT_FILES if not _remove_quietly(run_dir / name)]
        if survivors:
            # Louder, because this is the bad case: a document describing an earlier state of this
            # run is still on disk and `bl verify` will not look at it.
            print(
                f"[bounded-loops] could not write the receipt artifact ({type(exc).__name__}: "
                f"{exc}), AND could not remove {', '.join(survivors)}. Those files describe an "
                "EARLIER state of this run and must not be trusted or attached to anything. Delete "
                "them by hand, or run `bl receipt <run-dir>` to regenerate. The ledger is "
                "unaffected and remains the authoritative record.",
                file=sys.stderr,
            )
        else:
            print(
                f"[bounded-loops] could not write the receipt artifact ({type(exc).__name__}: "
                f"{exc}). Any earlier receipt here has been removed rather than left stale; run "
                "`bl receipt <run-dir>` to re-derive it. The ledger is unaffected and remains the "
                "authoritative record.",
                file=sys.stderr,
            )


