"""Hash-chain construction and verification for the append-only loop ledger.

The construction is standard tamper-evident logging: line *k* carries ``prev``, the
SHA-256 of line *k-1*'s exact bytes, and the first line carries a genesis value of
sixty-four zeros. We claim no part of the scheme.

Three choices in it are deliberate, and they are the reason this module exists rather
than a re-use of ``hash_chain_events.py``.

**The hash covers the stored line's bytes, not a re-serialisation of the parsed
entry.** A verifier therefore needs no agreement with the writer about canonical JSON
--- key order, separators and escaping are already fixed by what is on disk. The whole
procedure is then ten lines in any language with SHA-256 and a JSON parser::

    prev = "0" * 64
    for line in open("ledger.jsonl"):
        line = line.rstrip("\\n")
        assert json.loads(line)["prev"] == prev
        prev = hashlib.sha256(line.encode()).hexdigest()

That matters because the property being claimed is verifiability *by a third party*.
A scheme requiring the reader to reproduce our canonicalisation byte for byte is
verifiable only by us, which is a different and much weaker claim.

One caveat on that procedure, since claiming language-independence and then shipping a
Python-shaped recipe would be its own small version of this project's favourite defect:
the snippet relies on Python translating ``\\r\\n`` to ``\\n`` on read. A verifier written
in a language that reads bytes literally must strip a trailing ``\\r`` as well as the
``\\n``. The writer only ever emits ``\\n``, so this bites exactly one case --- a ledger
that crossed a platform through a tool that rewrote its line endings --- and in that case
the file has been rewritten, which the chain is entitled to notice. The rule for a
non-Python implementation is: hash the line's content with all trailing end-of-line bytes
removed.

**A ledger written before chaining existed reports UNCHAINED, not BROKEN.** Earlier
versions wrote no ``prev`` at all, and reporting those runs as tampered would be
false. The mixed case --- an unchained prefix followed by a chained suffix, which is
exactly what upgrading in place produces --- is reported as MIXED with the boundary
named, and the suffix is still verified: the first chained line's ``prev`` covers the
legacy tail's bytes, so the prefix cannot be edited without breaking the suffix.

**A partial final line is TORN_TAIL, not BROKEN.** A process killed mid-write leaves
one incomplete line, which is an interrupted run rather than an edited one. An
operator needs those two reported differently because only one of them is an
accusation.

What the chain does *not* do, stated here because this is where a reader will look for
it: there is no secret and no signature, so an adversary who can rewrite the whole
file can recompute every link and leave the ledger consistent. The chain is evidence
against anyone who can only append, and against anyone who edits without recomputing
--- which covers a torn write, a stray script, and an agent editing the audit trail
that judges it. Against a determined rewriter what remains is the head hash, and its
value is exactly the strength of the strongest witness holding a copy outside the run
directory. The controller therefore reports the head on every run, so an operator's
terminal and a CI log are both such witnesses.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from pathlib import Path
from typing import Sequence

#: Predecessor value carried by the first line of a chain.
GENESIS = "0" * 64

#: Ledger field holding the predecessor hash.
CHAIN_FIELD = "prev"


class ChainStatus(str, Enum):
    """Outcome of verifying one ledger file. Five states, each separately actionable."""

    #: Every line carries ``prev`` and every link matches.
    VERIFIED = "VERIFIED"
    #: No line carries ``prev``: written before chaining existed. Not verifiable.
    UNCHAINED = "UNCHAINED"
    #: Unchained prefix, verified chained suffix. The upgrade-in-place shape.
    MIXED = "MIXED"
    #: The prefix verifies; the final line is a partial write.
    TORN_TAIL = "TORN_TAIL"
    #: A link mismatch or a malformed line inside the chained region.
    BROKEN = "BROKEN"


@dataclass(frozen=True)
class ChainReport:
    """What a verifier learned, including how much of the file it actually covered."""

    status: ChainStatus
    #: Total lines present in the file.
    lines: int
    #: Lines covered by a verified link. Never inferred; counted.
    verified_lines: int
    #: SHA-256 of the last complete line, or GENESIS for an empty ledger. The value an
    #: external witness should hold.
    head: str
    #: 1-based line number where verification stopped, when it did.
    failed_at: int | None = None
    detail: str = ""

    @property
    def verified(self) -> bool:
        """True only for a fully chained, fully matching file.

        Deliberately excludes MIXED and UNCHAINED. A caller that wants to treat a
        legacy ledger as acceptable must say so at its own call site; it must not get
        there by reading a boolean that quietly folded three states into one.
        """
        return self.status is ChainStatus.VERIFIED

    @property
    def tampered(self) -> bool:
        """True only when a covered line failed its link. Excludes a torn tail."""
        return self.status is ChainStatus.BROKEN


def line_hash(line: str) -> str:
    """SHA-256 of one stored ledger line, excluding its terminating newline."""
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


def chain_ledger_lines(lines: Sequence[str]) -> ChainReport:
    """Verify a chain over already-split ledger lines."""
    verified = 0
    unchained_prefix = 0
    prev = GENESIS
    total = len(lines)

    for number, line in enumerate(lines, 1):
        if not line.strip():
            return ChainReport(
                status=ChainStatus.BROKEN,
                lines=total,
                verified_lines=verified,
                head=prev,
                failed_at=number,
                detail=f"blank line at {number}: the writer never emits one",
            )
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            if number == total:
                return ChainReport(
                    status=ChainStatus.TORN_TAIL,
                    lines=total,
                    verified_lines=verified,
                    head=prev,
                    failed_at=number,
                    detail=(
                        f"line {number} is not complete JSON, and it is the last line: "
                        "a run interrupted mid-write, not an edited ledger"
                    ),
                )
            return ChainReport(
                status=ChainStatus.BROKEN,
                lines=total,
                verified_lines=verified,
                head=prev,
                failed_at=number,
                detail=f"invalid JSON at line {number}: {exc.msg}",
            )
        if not isinstance(entry, dict):
            return ChainReport(
                status=ChainStatus.BROKEN,
                lines=total,
                verified_lines=verified,
                head=prev,
                failed_at=number,
                detail=f"line {number} is not a JSON object",
            )

        recorded = entry.get(CHAIN_FIELD)
        if recorded is None:
            if verified:
                # A chained line already verified, so this file was chained and is not
                # any longer. Stripping the field is the cheapest way to make an edit
                # look like a legacy file, and treating it as legacy is what would make
                # that work.
                return ChainReport(
                    status=ChainStatus.BROKEN,
                    lines=total,
                    verified_lines=verified,
                    head=prev,
                    failed_at=number,
                    detail=(
                        f"line {number} carries no {CHAIN_FIELD} but line "
                        f"{unchained_prefix + verified} did: the chain was removed"
                    ),
                )
            unchained_prefix += 1
            prev = line_hash(line)
            continue

        if recorded != prev:
            return ChainReport(
                status=ChainStatus.BROKEN,
                lines=total,
                verified_lines=verified,
                head=prev,
                failed_at=number,
                detail=(
                    f"{CHAIN_FIELD} mismatch at line {number}: the ledger records "
                    f"{_short(str(recorded))} but line {number - 1} hashes to {_short(prev)}"
                ),
            )
        verified += 1
        prev = line_hash(line)

    if total == 0:
        return ChainReport(
            status=ChainStatus.VERIFIED,
            lines=0,
            verified_lines=0,
            head=GENESIS,
            detail="empty ledger: nothing recorded, nothing to contradict",
        )
    if verified == 0:
        return ChainReport(
            status=ChainStatus.UNCHAINED,
            lines=total,
            verified_lines=0,
            head=prev,
            detail=(
                f"no line carries {CHAIN_FIELD}: written before 0.6.6, so integrity "
                "cannot be checked either way"
            ),
        )
    if unchained_prefix:
        return ChainReport(
            status=ChainStatus.MIXED,
            lines=total,
            verified_lines=verified,
            head=prev,
            detail=(
                f"lines 1-{unchained_prefix} predate chaining; lines "
                f"{unchained_prefix + 1}-{total} verify, and their chain covers the "
                "bytes of the unchained prefix"
            ),
        )
    return ChainReport(
        status=ChainStatus.VERIFIED,
        lines=total,
        verified_lines=verified,
        head=prev,
        detail=f"{verified} of {total} lines chained and matching",
    )


def verify_ledger_file(path: Path) -> ChainReport:
    """Verify the chain of a ledger on disk. Reads the file and nothing else."""
    if path.is_symlink():
        return ChainReport(
            status=ChainStatus.BROKEN,
            lines=0,
            verified_lines=0,
            head=GENESIS,
            failed_at=None,
            detail="ledger path is a symlink, so what was verified is not what was written",
        )
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ChainReport(
            status=ChainStatus.BROKEN,
            lines=0,
            verified_lines=0,
            head=GENESIS,
            detail=f"no ledger at {path}",
        )
    except OSError as exc:
        return ChainReport(
            status=ChainStatus.BROKEN,
            lines=0,
            verified_lines=0,
            head=GENESIS,
            detail=f"cannot read ledger: {exc}",
        )
    except UnicodeDecodeError as exc:
        # A ledger that is not valid UTF-8 IS a broken chain, and saying so is the whole job of this
        # function. Until now `bl verify` raised UnicodeDecodeError out of `main` and printed a
        # traceback: the tool whose purpose is to survive a tampered record was defeated by one.
        # `UnicodeDecodeError` subclasses ValueError, not OSError, so the handler above never saw it.
        return ChainReport(
            status=ChainStatus.BROKEN,
            lines=0,
            verified_lines=0,
            head=GENESIS,
            detail=f"ledger is not valid UTF-8, so its bytes are not the record it claims: {exc}",
        )
    return chain_ledger_lines(text.splitlines())


def head_of_lines(lines: Sequence[str]) -> str:
    """Predecessor hash the next appended line must carry."""
    for line in reversed(lines):
        if line.strip():
            return line_hash(line)
    return GENESIS


def _short(digest: str) -> str:
    """First twelve hex characters: enough to compare by eye, short enough to read."""
    return digest[:12] if len(digest) > 12 else digest
