import json
from typing import Literal

from bounded_loops.adapters.io.file_ledger import FileLedger, _deserialise
from bounded_loops.adapters.io.clock import UtcClock
from bounded_loops.domain.models import LedgerEntry, Verdict

_Decision = Literal["continue", "done", "halt", "pause", "killed", "error"]


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


def test_recorded_evidence_is_unchanged_after_source_mutates(tmp_path):
    evidence = {"checks": [{"name": "pytest", "count": 1}]}
    budget = {"tokens": {"per_lap": [100]}}
    entry = LedgerEntry(
        lap=1,
        ts=UtcClock().now_iso(),
        verdict=Verdict(passed=True, detail="ok", evidence=evidence),
        decision="done",
        budget_spent=budget,
    )
    ledger = FileLedger(tmp_path / "ledger.jsonl")
    ledger.record(entry)

    evidence["checks"][0]["count"] = 999
    budget["tokens"]["per_lap"].append(999)

    payload = json.loads(ledger.path().read_text().strip())
    assert payload["verdict"]["evidence"] == {"checks": [{"name": "pytest", "count": 1}]}
    assert payload["budget_spent"] == {"tokens": {"per_lap": [100]}}


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


# --- Gate provenance: WHICH gate produced the verdict ------------------------
#
# A verdict is only reviewable if the receipt says what decided it. This field is a SIBLING of
# "verdict", never a member of it, because everything inside "verdict" is authored by the gate and
# provenance a gate can write is a claim rather than provenance.


def test_the_ledger_records_which_gate_produced_the_verdict(tmp_path):
    ledger = FileLedger(tmp_path / "ledger.jsonl")
    ledger.record(LedgerEntry(
        lap=1, ts=UtcClock().now_iso(), verdict=Verdict(True, "ok"), decision="done",
        budget_spent={"laps": 1},
        gate={"kind": "acme-check", "source": "plugin", "distribution": "acme-gates"},
    ))

    row = json.loads((tmp_path / "ledger.jsonl").read_text().splitlines()[0])
    assert row["gate"] == {
        "kind": "acme-check", "source": "plugin", "distribution": "acme-gates",
    }
    assert "gate" not in row["verdict"], (
        "provenance landed inside the gate-authored verdict, where the gate could forge it"
    )


def test_the_serialised_ledger_row_has_exactly_the_expected_keys(tmp_path):
    """Pins the on-disk shape. Nothing pinned it before, and the ledger is a COMPATIBILITY
    surface: `bl verify` must read run directories written by older versions, so a key
    appearing or vanishing unnoticed is how that promise breaks quietly."""
    ledger = FileLedger(tmp_path / "ledger.jsonl")
    ledger.record(LedgerEntry(
        lap=1, ts=UtcClock().now_iso(), verdict=Verdict(True, "ok"), decision="done",
        budget_spent={"laps": 1},
    ))

    row = json.loads((tmp_path / "ledger.jsonl").read_text().splitlines()[0])
    assert set(row) == {
        "prev", "lap", "ts", "verdict", "decision", "budget_spent", "attempted", "handoff", "gate",
    }
    assert set(row["verdict"]) == {"passed", "detail", "evidence"}


def test_a_ledger_written_before_the_gate_field_existed_still_verifies_and_loads(tmp_path):
    """The compatibility promise, tested against a hand-built OLD row rather than a fixture this
    version produced — a fixture regenerated by the current serialiser would test nothing.

    Two halves, and the second is the one that matters: the chain hashes RAW LINE TEXT, so a line
    written without this key is unchanged and still verifies; and `_deserialise` reads the key with
    a default, so the row still loads. Either half failing makes every pre-existing run directory
    unreadable by the tool whose job is to audit it.
    """
    from bounded_loops.adapters.io.ledger_chain import verify_ledger_file

    old_row = {
        "prev": "0" * 64, "lap": 1, "ts": "2026-01-01T00:00:00Z",
        "verdict": {"passed": True, "detail": "pytest: 42 passed", "evidence": {"code": 0}},
        "decision": "done", "budget_spent": {"laps": 1}, "attempted": True, "handoff": "",
    }
    line = json.dumps(old_row, ensure_ascii=False, separators=(",", ":"))
    (tmp_path / "ledger.jsonl").write_text(line + "\n")

    assert verify_ledger_file(tmp_path / "ledger.jsonl").verified
    assert dict(_deserialise(line).gate) == {}, "an old row must load with no provenance, not crash"


def test_gate_provenance_is_frozen_on_the_entry():
    """`budget_spent` is frozen because a caller-owned mapping could rewrite an already-recorded
    fact. Provenance is recorded for exactly the same reason and gets the same treatment."""
    entry = LedgerEntry(
        lap=1, ts="2026-01-01T00:00:00Z", verdict=Verdict(True, "ok"), decision="done",
        budget_spent={"laps": 1}, gate={"kind": "command"},
    )
    try:
        entry.gate["kind"] = "forged"          # type: ignore[index]
    except TypeError:
        return
    raise AssertionError("provenance was mutable after the entry was built")
