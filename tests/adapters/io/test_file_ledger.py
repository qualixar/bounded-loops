import json
from typing import Literal

from bounded_loops.adapters.io.file_ledger import FileLedger, _deserialise
from bounded_loops.adapters.io.clock import UtcClock
from bounded_loops.domain.models import LedgerEntry, Verdict

_Decision = Literal["continue", "done", "halt", "pause", "killed"]


def _make_entry(lap: int, passed: bool, decision: _Decision) -> LedgerEntry:
    return LedgerEntry(
        lap=lap,
        ts=UtcClock().now_iso(),
        verdict=Verdict(passed=passed, detail="test", evidence={"code": 0}),
        decision=decision,
        budget_spent={"laps": lap, "tokens": 100 * lap, "wallclock_s": lap * 5},
    )


# --- Append-only invariant --------------------------------------------------

def test_record_appends_not_overwrites(tmp_path):
    ledger = FileLedger(tmp_path / "ledger.jsonl")
    e1 = _make_entry(1, False, "continue")
    e2 = _make_entry(2, True,  "done")
    ledger.record(e1)
    ledger.record(e2)
    lines = ledger.path().read_text().splitlines()
    assert len(lines) == 2, "Expected exactly 2 lines"


def test_record_does_not_clear_previous_entries(tmp_path):
    ledger = FileLedger(tmp_path / "ledger.jsonl")
    e1 = _make_entry(1, False, "continue")
    ledger.record(e1)
    # Re-open a second FileLedger to the same file (simulates engine restart).
    ledger2 = FileLedger(tmp_path / "ledger.jsonl")
    e2 = _make_entry(2, True, "done")
    ledger2.record(e2)
    lines = (tmp_path / "ledger.jsonl").read_text().splitlines()
    assert len(lines) == 2, "Second open must not clear file"
    assert json.loads(lines[0])["lap"] == 1, "First entry must survive"


def test_each_line_is_valid_json(tmp_path):
    ledger = FileLedger(tmp_path / "ledger.jsonl")
    for i in range(1, 4):
        ledger.record(_make_entry(i, i % 2 == 0, "continue"))
    for line in ledger.path().read_text().splitlines():
        json.loads(line)  # raises on invalid JSON


# --- LedgerEntry round-trip -------------------------------------------------

def test_ledger_entry_round_trips(tmp_path):
    ledger = FileLedger(tmp_path / "ledger.jsonl")
    original = _make_entry(1, True, "done")
    ledger.record(original)
    line = ledger.path().read_text().strip()
    recovered = _deserialise(line)
    assert recovered.lap == original.lap
    assert recovered.ts  == original.ts
    assert recovered.verdict.passed  == original.verdict.passed
    assert recovered.verdict.detail  == original.verdict.detail
    assert recovered.verdict.evidence == original.verdict.evidence
    assert recovered.decision == original.decision
    assert recovered.budget_spent == original.budget_spent


def test_unicode_evidence_survives_round_trip(tmp_path):
    entry = LedgerEntry(
        lap=1,
        ts=UtcClock().now_iso(),
        verdict=Verdict(passed=False, detail="文字化け test", evidence={"msg": "日本語"}),
        decision="continue",
        budget_spent={"laps": 1, "tokens": 0, "wallclock_s": 0},
    )
    ledger = FileLedger(tmp_path / "ledger.jsonl")
    ledger.record(entry)
    recovered = _deserialise(ledger.path().read_text().strip())
    assert recovered.verdict.detail == "文字化け test"


# --- path() -----------------------------------------------------------------

def test_path_returns_the_ledger_file(tmp_path):
    p = tmp_path / "sub" / "run.jsonl"
    ledger = FileLedger(p)
    assert ledger.path() == p
    assert ledger.path().exists()


# --- lines end with newline ------------------------------------------------

def test_each_entry_ends_with_newline(tmp_path):
    ledger = FileLedger(tmp_path / "ledger.jsonl")
    ledger.record(_make_entry(1, True, "done"))
    raw = ledger.path().read_bytes()
    assert raw.endswith(b"\n"), "JSONL spec: each line ends with LF"
