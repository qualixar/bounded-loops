"""``bl verify`` — check a run's receipt without running anything.

The soundness argument's practical content is that a third party holding only a run
directory can confirm the claim. That was true of the data and false of the tooling:
nothing shipped let anyone do it, so the property was declared and not exercisable —
the same shape as a wallclock ceiling that is validated, displayed, and never
enforced. This command is the exercise.

It reads. It never writes, never re-runs a gate, and never consults the engine that
produced the run. Three checks, reported separately because they fail for different
reasons and an operator's next action differs:

1. **Chain** — is the ledger internally consistent (`ledger_chain`)?
2. **Anchor** — does the head match the head recorded when the run ended? An
   operator holding the head from their terminal or CI log can pass `--expect-head`
   and get the stronger check, since the recorded copy shares a filesystem with the
   ledger and so shares its adversary.
3. **Completeness** — does the ledger account for every lap the receipt claims? This
   is the hypothesis the chain-integrity result needs and that hashing alone does not
   supply: a truncated ledger is a well-formed prefix, and only an independent record
   of the expected length turns it back into a detectable edit.

Exit status is 0 only when every applicable check passes. A verifier that exits 0 on
"could not tell" is worse than no verifier, because it converts absence of evidence
into a passing build step.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from bounded_loops.adapters.io.ledger_chain import ChainStatus, verify_ledger_file


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "verify",
        help="Verify a run receipt: ledger chain, recorded head, and lap accounting.",
        description=(
            "Reads a run directory (or a ledger file) and reports whether its "
            "append-only ledger is intact. Reads only; never re-runs a gate."
        ),
    )
    parser.add_argument(
        "target", type=Path,
        help="A run directory containing ledger.jsonl, or a path to a ledger file.",
    )
    parser.add_argument(
        "--expect-head", default=None, metavar="SHA256",
        help=(
            "The head digest you recorded when the run ended, from the run's output. "
            "Supplying it is the only check an adversary with write access to the "
            "whole run directory cannot satisfy."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON output.")
    parser.set_defaults(func=_cmd_verify)


def _cmd_verify(args: argparse.Namespace) -> int:
    ledger_path, metadata = _resolve(args.target)
    report = verify_ledger_file(ledger_path)

    checks: list[dict] = [{
        "check": "chain",
        "passed": report.verified,
        "status": report.status.value,
        "detail": report.detail,
    }]

    expected = args.expect_head or (metadata or {}).get("ledger_head") or ""
    external = bool(args.expect_head)
    source = "--expect-head" if external else "the run receipt"
    if expected:
        matched = expected == report.head
        # A co-located witness and an external one are NOT the same finding, and printing
        # both as "MATCH" was the defect an enterprise-architecture review caught in this
        # command: the receipt sits in the directory it vouches for, so anyone who edits
        # the ledger can edit the receipt in the same pass and collect a green tick. The
        # check still passes — it detects the careless editor, which is most of them — but
        # it reports its own strength rather than letting a reader infer the stronger one.
        checks.append({
            "check": "anchor",
            "passed": matched,
            "status": ("MATCH_EXTERNAL" if external else "MATCH_COLOCATED") if matched
                      else "MISMATCH",
            "witness": "external" if external else "co-located",
            "detail": (
                (
                    "head matches the digest you supplied. This is the strong check ONLY IF you "
                    "kept that digest outside this directory: verify cannot see where you got it, "
                    "and a digest copied out of the run directory proves nothing about an editor "
                    "of that directory"
                    if external else
                    f"head matches the digest in {source} — but that file sits beside the "
                    f"ledger, so an editor of both satisfies this. Re-run with "
                    f"--expect-head <digest> from your terminal or CI log for the strong check"
                )
                if matched
                else f"{source} records {expected} but the ledger now heads at {report.head}"
            ),
        })
    else:
        checks.append({
            "check": "anchor",
            "passed": False,
            "status": "NO_WITNESS",
            "detail": (
                "no recorded head to compare against, so the chain is only evidence "
                "against an adversary who cannot recompute it: pass --expect-head "
                "with the digest printed when the run ended"
            ),
        })

    # `ledger_rows` in preference to `laps`. `--resume` restarts the in-process lap counter while the
    # ledger stays append-only, so `laps` undercounts a resumed run by every earlier segment: four
    # hashed rows beside `laps: 2` passed as COMPLETE, and the slack is exactly where a removed tail
    # would hide. `laps` remains the fallback so run directories written before `ledger_rows` existed
    # still get the check they always had — a weakened witness beats none.
    recorded_rows = (metadata or {}).get("ledger_rows")
    claimed = recorded_rows if isinstance(recorded_rows, int) else (metadata or {}).get("laps")
    exact = isinstance(recorded_rows, int)
    if isinstance(claimed, int):
        # A run records one ledger row per lap within a segment. Fewer rows than recorded is a
        # removed tail, which is exactly the case a hash chain cannot see on its own.
        passed = report.lines >= claimed
        unit = "recorded rows" if exact else "claimed laps (no row count recorded)"
        checks.append({
            "check": "completeness",
            "passed": passed,
            "status": "COMPLETE" if passed else "TRUNCATED",
            "witness": "row-count" if exact else "lap-count",
            "detail": (
                f"{report.lines} ledger rows for {claimed} {unit}"
                if passed
                else f"the run recorded {claimed} rows but the ledger holds {report.lines}"
            ),
        })
    else:
        checks.append({
            "check": "completeness",
            "passed": False,
            "status": "NO_RECEIPT",
            "detail": (
                "no run metadata beside the ledger, so a removed tail would present "
                "as a shorter run: verify against a receipt written with --run-id"
            ),
        })

    ok = all(check["passed"] for check in checks)
    if args.json:
        print(json.dumps({
            "subcommand": "verify",
            "ledger_path": str(ledger_path),
            "verified": ok,
            "head": report.head,
            "lines": report.lines,
            "verified_lines": report.verified_lines,
            "checks": checks,
        }))
    else:
        _print_human(ledger_path, report, checks, ok=ok)
    return 0 if ok else 1


def _print_human(ledger_path: Path, report, checks: list[dict], *, ok: bool) -> None:
    print(f"Ledger: {ledger_path}")
    print(f"Head:   {report.head}")
    print(f"Rows:   {report.lines} ({report.verified_lines} covered by the chain)")
    print("")
    for check in checks:
        symbol = "✓" if check["passed"] else "✗"
        print(f"{symbol} {check['check']:<13} {check['status']}")
        print(f"  {check['detail']}")
    print("")
    if ok:
        anchor = next((c for c in checks if c["check"] == "anchor"), {})
        if anchor.get("witness") == "external":
            # States the CONDITION rather than asserting it. The previous wording claimed the
            # digest came "from outside the run directory", which verify cannot know — it sees a
            # command-line argument, not where the caller found it. A receipt that published a
            # pasteable head from inside the directory turned that assertion into a false green.
            print("Verified: this run's LEDGER is intact and accounts for every lap it claims, and "
                  "its head matches the digest you supplied.")
            print("  Not checked: receipt.md / receipt.json, which are renderings of the ledger "
                  "and are not covered by its hash chain.")
            print("  Established only if that digest was kept outside this run directory. "
                  "A digest read out of the directory cannot show it was not rewritten wholesale.")
        else:
            print("Verified: this run's LEDGER is internally consistent and accounts for every lap "
                  "it claims.")
            print("  Not established: that the run directory was not rewritten wholesale. "
                  "Only --expect-head with a digest kept elsewhere can show that.")
        return
    if report.status is ChainStatus.BROKEN:
        # BROKEN covers three unrelated situations and only one of them is an accusation.
        # Printing "edited after it was written" for a mistyped path told a user their run
        # had been tampered with when no such run existed — caught by a usability review.
        if "no ledger" in report.detail:
            print("NOT VERIFIED: no ledger at this path. Is this a run directory?")
            print("  Try `bl runs <loop-dir>` to list runs, or pass a ledger file directly.")
        elif "symlink" in report.detail:
            print("NOT VERIFIED: the ledger path is a symlink, so the bytes checked are not "
                  "necessarily the bytes that were written.")
        elif "cannot read" in report.detail:
            print("NOT VERIFIED: the ledger could not be read. See the reason above.")
        else:
            print("NOT VERIFIED: the ledger was edited after it was written.")
    elif report.status is ChainStatus.TORN_TAIL:
        print("NOT VERIFIED: the final row is a partial write — an interrupted run.")
    elif report.status is ChainStatus.UNCHAINED:
        print("NOT VERIFIED: written before 0.6.6, which is when chaining began.")
    elif report.status is ChainStatus.MIXED:
        print("NOT VERIFIED in full: an unchained prefix predates chaining.")
    else:
        print("NOT VERIFIED: one check could not be satisfied. See above.")


def _resolve(target: Path) -> tuple[Path, dict | None]:
    """Accept a run directory or a ledger file; return the ledger and any receipt."""
    if target.is_dir():
        ledger = target / "ledger.jsonl"
        if not ledger.exists():
            legacy = target / ".ledger.jsonl"
            if legacy.exists():
                ledger = legacy
        metadata_path = target / "metadata.json"
    else:
        ledger = target
        metadata_path = target.parent / "metadata.json"

    metadata: dict | None = None
    if metadata_path.is_file() and not metadata_path.is_symlink():
        try:
            loaded = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            metadata = None
        else:
            metadata = loaded if isinstance(loaded, dict) else None
    return ledger, metadata
