"""
FileLedger — concrete `LedgerPort` adapter.

Append-only, hash-chained, JSON-Lines verdict ledger. Every `record()` call
serialises one `LedgerEntry` as a single JSON object carrying `prev` — the
SHA-256 of the preceding line's bytes — and appends it. The file is NEVER
rewritten or truncated; only opened in append mode. Each line is flushed and
fsynced so a crash after line N cannot corrupt lines 0..N-1
(https://jsonlines.org best practice).

Append-only was never tamper-evidence. Opening a file exclusively in mode
'a' constrains this writer; it constrains nothing about anyone else holding a
path to the file, and the ledger sits next to a workspace an agent can write.
The chain is what makes an edit detectable, and `ledger_chain` documents
precisely how far that goes. Version 0.6.6 is where the chain starts: the paper
had stated a chain-integrity theorem since its first draft while this adapter
wrote no hash at all, which is the same declared-but-not-enforced defect the
paper is about, found in the artifact the paper's own evidence rests on.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

from bounded_loops.adapters.io.file_lock import locked_file
from bounded_loops.adapters.io.ledger_chain import (
    CHAIN_FIELD,
    GENESIS,
    head_of_lines,
)
from bounded_loops.domain.errors import EvidenceError
from bounded_loops.domain.models import LedgerEntry, Verdict


class FileLedger:
    """
    Implements LedgerPort.
    Appends one hash-chained JSONL line per LedgerEntry to <ledger_path>.
    NEVER rewrites or truncates the file.
    """

    def __init__(self, ledger_path: Path) -> None:
        self._path: Path = ledger_path
        # Ensure parent directory exists.
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Touch file to create it if absent (so path() is always valid).
        if not self._path.exists():
            self._path.touch()

    def record(self, entry: LedgerEntry) -> None:
        """Chain one entry onto the tail and append it. Flushed and fsynced.

        The predecessor hash is read from the file inside the lock rather than
        cached in memory, because two controllers sharing a loop-level ledger
        would otherwise each extend the same predecessor and fork the chain —
        and a forked chain reads as tampering. A false accusation of tampering
        costs more than no chain at all, so the read is not optimised away.
        """
        if self._path.is_symlink():
            raise EvidenceError("ledger path must not be a symlink")
        try:
            with locked_file(self._lock_path(), exclusive=True):
                line = _serialise(entry, prev=self._head_unlocked())
                # Open in append+text mode. Do NOT use 'w' anywhere in this class.
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
                    fh.flush()  # force OS write; prevents partial line on crash
                    os.fsync(fh.fileno())  # survive power loss, not just process exit
        except OSError as exc:
            raise EvidenceError(f"cannot append to ledger: {exc}") from exc

    def head(self) -> str:
        """Hash of the last recorded line, or GENESIS when nothing is recorded.

        This is the value worth holding outside the run directory: the chain
        detects edits by anyone who cannot recompute the suffix, and a witness to
        the head is what makes recomputing it insufficient.
        """
        try:
            with locked_file(self._lock_path(), exclusive=False):
                return self._head_unlocked()
        except OSError as exc:
            raise EvidenceError(f"cannot read ledger head: {exc}") from exc

    def path(self) -> Path:
        return self._path

    def _lock_path(self) -> Path:
        """Lock the ledger itself rather than a sidecar `.lock` file.

        The graph event log uses a sidecar because it locks around reads that never
        open the target for writing. This ledger opens the target in append mode
        anyway, so a sidecar would buy nothing and cost something real: a loop-level
        ledger lives inside the loop package, so `.ledger.jsonl.lock` would appear in
        the author's repository, in `git status`, and — since only `.ledger.jsonl` is
        ignored — in a commit. Caught by staging it by accident.
        """
        return self._path

    def _head_unlocked(self) -> str:
        # Reads the whole file. A run's ledger holds at most max_iterations + 1
        # rows, so this is bounded by the same declared number that bounds the run.
        try:
            return head_of_lines(self._path.read_text(encoding="utf-8").splitlines())
        except FileNotFoundError:
            return GENESIS


def _serialise(entry: LedgerEntry, *, prev: str = GENESIS) -> str:
    """Convert LedgerEntry -> compact JSON string (one line, no indent)."""
    d = {
        # Predecessor hash first, so a reader eyeballing the file sees the link
        # before the content it links.
        CHAIN_FIELD: prev,
        "lap": entry.lap,
        "ts": entry.ts,
        "verdict": {
            "passed": entry.verdict.passed,
            "detail": entry.verdict.detail,
            "evidence": _json_value(entry.verdict.evidence),
        },
        "decision": entry.decision,
        "budget_spent": _json_value(entry.budget_spent),
        # Attempts, not laps, are what a budget is denominated in. See LedgerEntry.attempted.
        "attempted": bool(entry.attempted),
        # Names the handoff file when a bound wound the run down, so the receipt points at the
        # evidence rather than leaving a reader to know the convention.
        "handoff": str(entry.handoff or ""),
    }
    return json.dumps(d, ensure_ascii=False, separators=(",", ":"))


def _json_value(value: object) -> object:
    """Convert immutable domain metadata back to JSON-compatible primitives."""
    if isinstance(value, Mapping):
        return {key: _json_value(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_json_value(child) for child in value]
    if isinstance(value, frozenset):
        return sorted((_json_value(child) for child in value), key=repr)
    return value


def _deserialise(line: str) -> LedgerEntry:
    """Round-trip helper: JSON line -> LedgerEntry (used in tests, not the main path).

    Reads every field the serialiser writes. It previously dropped `attempted` and
    `handoff`, which is the kind of omission that lets a round-trip test pass on a
    field it never carried — and `attempted` is the field the utilisation figures in
    the paper are computed from.
    """
    d = json.loads(line)
    verdict_d = d["verdict"]
    verdict = Verdict(
        passed=verdict_d["passed"],
        detail=verdict_d["detail"],
        evidence=verdict_d.get("evidence", {}),
    )
    return LedgerEntry(
        lap=d["lap"],
        ts=d["ts"],
        verdict=verdict,
        decision=d["decision"],
        budget_spent=d["budget_spent"],
        attempted=bool(d.get("attempted", True)),
        handoff=str(d.get("handoff", "")),
    )
