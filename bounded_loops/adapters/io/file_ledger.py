"""
FileLedger — concrete `LedgerPort` adapter.

Append-only, JSON-Lines verdict ledger. Every `record()` call serialises
one `LedgerEntry` as a single JSON object and appends it to a `.jsonl`
file. The file is NEVER rewritten or truncated — only opened in append
mode. Each line is flushed immediately so a crash after line N cannot
corrupt lines 0..N-1 (https://jsonlines.org best practice).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from bounded_loops.domain.models import LedgerEntry, Verdict


class FileLedger:
    """
    Implements LedgerPort.
    Appends one JSONL line per LedgerEntry to <ledger_path>.
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
        """Serialise entry as JSON and append one line. Flush immediately."""
        line = _serialise(entry)
        # Open in append+text mode. Do NOT use 'w' anywhere in this class.
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()  # force OS write; prevents partial line on crash

    def path(self) -> Path:
        return self._path


def _serialise(entry: LedgerEntry) -> str:
    """Convert LedgerEntry -> compact JSON string (one line, no indent)."""
    d = {
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
    """Round-trip helper: JSON line -> LedgerEntry (used in tests, not the main path)."""
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
    )
