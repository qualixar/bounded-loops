"""The ledger's chain must detect an edited row, and must not cry wolf.

SEC-01. The paper has stated a chain-integrity theorem since its first draft, and
`FileLedger` wrote no hash at all: `grep -cE "hash|prev_|chain|sha256" file_ledger.py`
returned 0 on the released 0.6.5. These tests are the enforcement the theorem was
missing.

Each test names the adversary or the accident it stands for, because a tamper-evidence
suite that only proves "the happy path writes a field" proves nothing about tampering.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path

import pytest

from bounded_loops.adapters.io.file_ledger import FileLedger, _deserialise
from bounded_loops.adapters.io.ledger_chain import (
    CHAIN_FIELD,
    GENESIS,
    ChainStatus,
    chain_ledger_lines,
    line_hash,
    verify_ledger_file,
)
from bounded_loops.domain.errors import EvidenceError
from bounded_loops.domain.models import LedgerEntry, Verdict


def _entry(lap: int, *, passed: bool = True, attempted: bool = True) -> LedgerEntry:
    return LedgerEntry(
        lap=lap,
        ts=f"2026-08-17T00:00:{lap:02d}Z",
        verdict=Verdict(passed=passed, detail=f"lap {lap}", evidence={"n": lap}),
        decision="continue",
        budget_spent={"laps": lap},
        attempted=attempted,
    )


def _write(path: Path, laps: int = 3) -> FileLedger:
    ledger = FileLedger(path)
    for lap in range(1, laps + 1):
        ledger.record(_entry(lap))
    return ledger


# ── the chain is actually written ────────────────────────────────────────────────


def test_first_line_carries_the_genesis_predecessor(tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path / "ledger.jsonl")
    ledger.record(_entry(1))
    row = json.loads(ledger.path().read_text(encoding="utf-8").splitlines()[0])
    assert row[CHAIN_FIELD] == GENESIS


def test_each_line_carries_the_hash_of_the_previous_line_bytes(tmp_path: Path) -> None:
    ledger = _write(tmp_path / "ledger.jsonl", laps=4)
    lines = ledger.path().read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4
    for index in range(1, len(lines)):
        expected = hashlib.sha256(lines[index - 1].encode("utf-8")).hexdigest()
        assert json.loads(lines[index])[CHAIN_FIELD] == expected


def test_head_matches_the_hash_of_the_last_line(tmp_path: Path) -> None:
    ledger = _write(tmp_path / "ledger.jsonl", laps=2)
    last = ledger.path().read_text(encoding="utf-8").splitlines()[-1]
    assert ledger.head() == line_hash(last)


def test_head_of_an_untouched_ledger_is_genesis(tmp_path: Path) -> None:
    assert FileLedger(tmp_path / "ledger.jsonl").head() == GENESIS


def test_a_reopened_ledger_continues_the_chain_rather_than_restarting_it(
    tmp_path: Path,
) -> None:
    """The engine-restart case: a second FileLedger on the same file must not fork."""
    path = tmp_path / "ledger.jsonl"
    _write(path, laps=2)
    FileLedger(path).record(_entry(3))
    assert verify_ledger_file(path).status is ChainStatus.VERIFIED


# ── the chain detects what it claims to detect ───────────────────────────────────


def test_a_verified_ledger_reports_verified_with_every_line_covered(tmp_path: Path) -> None:
    report = verify_ledger_file(_write(tmp_path / "ledger.jsonl", laps=5).path())
    assert report.status is ChainStatus.VERIFIED
    assert report.verified and not report.tampered
    assert (report.lines, report.verified_lines) == (5, 5)


def test_editing_a_middle_row_is_detected_at_the_following_line(tmp_path: Path) -> None:
    """The agent-edits-its-own-verdict case: flip a Fail to a Pass in place."""
    path = tmp_path / "ledger.jsonl"
    _write(path, laps=4)
    lines = path.read_text(encoding="utf-8").splitlines()
    forged = json.loads(lines[1])
    forged["verdict"]["passed"] = False
    lines[1] = json.dumps(forged, ensure_ascii=False, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    report = verify_ledger_file(path)
    assert report.tampered and report.status is ChainStatus.BROKEN
    assert report.failed_at == 3, "the break surfaces at the successor of the edited row"
    assert CHAIN_FIELD in report.detail


def test_an_edit_that_keeps_the_same_length_is_still_detected(tmp_path: Path) -> None:
    """No length oracle: swapping one character inside a row breaks the chain."""
    path = tmp_path / "ledger.jsonl"
    _write(path, laps=3)
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[0] = lines[0].replace('"lap 1"', '"lap X"')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert verify_ledger_file(path).failed_at == 2


def test_stripping_the_chain_field_from_the_tail_is_not_mistaken_for_a_legacy_file(
    tmp_path: Path,
) -> None:
    """The downgrade attack. Removing `prev` must not buy the softer verdict."""
    path = tmp_path / "ledger.jsonl"
    _write(path, laps=3)
    lines = path.read_text(encoding="utf-8").splitlines()
    tail = json.loads(lines[-1])
    tail.pop(CHAIN_FIELD)
    lines[-1] = json.dumps(tail, ensure_ascii=False, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    report = verify_ledger_file(path)
    assert report.status is ChainStatus.BROKEN
    assert "the chain was removed" in report.detail


def test_deleting_a_row_is_detected(tmp_path: Path) -> None:
    """Truncation from the middle: the survivor's predecessor no longer exists."""
    path = tmp_path / "ledger.jsonl"
    _write(path, laps=4)
    lines = path.read_text(encoding="utf-8").splitlines()
    del lines[2]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert verify_ledger_file(path).status is ChainStatus.BROKEN


def test_reordering_two_rows_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    _write(path, laps=4)
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[1], lines[2] = lines[2], lines[1]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert verify_ledger_file(path).status is ChainStatus.BROKEN


def test_a_rewriter_who_recomputes_every_link_is_not_detected(tmp_path: Path) -> None:
    """The limit of the construction, asserted rather than left to a docstring.

    There is no secret, so whole-file rewriting is undetectable from the file alone.
    The head hash is what an external witness holds; this test pins the fact that
    without such a witness the chain says nothing about this adversary.
    """
    path = tmp_path / "ledger.jsonl"
    _write(path, laps=3)
    original_head = verify_ledger_file(path).head

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows[0]["verdict"]["passed"] = False
    rebuilt: list[str] = []
    prev = GENESIS
    for row in rows:
        row[CHAIN_FIELD] = prev
        line = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
        rebuilt.append(line)
        prev = line_hash(line)
    path.write_text("\n".join(rebuilt) + "\n", encoding="utf-8")

    report = verify_ledger_file(path)
    assert report.status is ChainStatus.VERIFIED, "the file is self-consistent again"
    assert report.head != original_head, "and only a witness to the head can see it"


# ── the chain does not cry wolf ──────────────────────────────────────────────────


def test_a_pre_chain_ledger_reports_unchained_not_broken(tmp_path: Path) -> None:
    """Runs recorded by 0.6.5 and earlier. Calling them tampered would be false."""
    path = tmp_path / "ledger.jsonl"
    legacy = [
        json.dumps({"lap": lap, "ts": "t", "verdict": {"passed": True, "detail": "",
                    "evidence": {}}, "decision": "continue", "budget_spent": {}})
        for lap in (1, 2)
    ]
    path.write_text("\n".join(legacy) + "\n", encoding="utf-8")

    report = verify_ledger_file(path)
    assert report.status is ChainStatus.UNCHAINED
    assert not report.verified and not report.tampered
    assert report.verified_lines == 0


def test_upgrading_in_place_reports_mixed_and_still_verifies_the_new_suffix(
    tmp_path: Path,
) -> None:
    """A loop-level ledger written by 0.6.5 and appended to by this version."""
    path = tmp_path / "ledger.jsonl"
    path.write_text(
        json.dumps({"lap": 1, "ts": "t", "verdict": {"passed": False, "detail": "",
                    "evidence": {}}, "decision": "continue", "budget_spent": {}}) + "\n",
        encoding="utf-8",
    )
    FileLedger(path).record(_entry(2))
    FileLedger(path).record(_entry(3))

    report = verify_ledger_file(path)
    assert report.status is ChainStatus.MIXED
    assert (report.lines, report.verified_lines) == (3, 2)
    assert not report.verified, "a legacy prefix is not covered and must not read as covered"


def test_editing_the_legacy_prefix_breaks_the_chained_suffix(tmp_path: Path) -> None:
    """What MIXED still buys: the suffix's first link covers the prefix's bytes."""
    path = tmp_path / "ledger.jsonl"
    path.write_text(
        json.dumps({"lap": 1, "ts": "t", "verdict": {"passed": False, "detail": "",
                    "evidence": {}}, "decision": "continue", "budget_spent": {}}) + "\n",
        encoding="utf-8",
    )
    FileLedger(path).record(_entry(2))
    lines = path.read_text(encoding="utf-8").splitlines()
    forged = json.loads(lines[0])
    forged["verdict"]["passed"] = True
    edited = json.dumps(forged)
    assert edited != lines[0], "the edit must actually change the bytes to prove anything"
    path.write_text("\n".join([edited, lines[1]]) + "\n", encoding="utf-8")

    report = verify_ledger_file(path)
    assert report.status is ChainStatus.BROKEN
    assert report.failed_at == 2


def test_a_torn_final_line_is_reported_as_interrupted_not_as_edited(tmp_path: Path) -> None:
    """A process killed mid-write. An operator must not read that as an accusation."""
    path = tmp_path / "ledger.jsonl"
    _write(path, laps=3)
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"prev":"' + "0" * 64 + '","lap":4,"ts":"2026')

    report = verify_ledger_file(path)
    assert report.status is ChainStatus.TORN_TAIL
    assert not report.tampered
    assert report.verified_lines == 3, "everything before the tear is still covered"


def test_an_empty_ledger_verifies_vacuously_and_says_so(tmp_path: Path) -> None:
    report = verify_ledger_file(FileLedger(tmp_path / "ledger.jsonl").path())
    assert report.status is ChainStatus.VERIFIED
    assert (report.lines, report.head) == (0, GENESIS)
    assert "nothing recorded" in report.detail


def test_a_missing_ledger_is_broken_rather_than_silently_empty(tmp_path: Path) -> None:
    report = verify_ledger_file(tmp_path / "absent.jsonl")
    assert report.status is ChainStatus.BROKEN
    assert "no ledger" in report.detail


def test_a_symlinked_ledger_is_refused_by_both_writer_and_verifier(tmp_path: Path) -> None:
    """Verifying through a symlink verifies bytes that are not the ones written."""
    real = tmp_path / "real.jsonl"
    real.touch()
    link = tmp_path / "link.jsonl"
    link.symlink_to(real)

    assert verify_ledger_file(link).status is ChainStatus.BROKEN
    with pytest.raises(EvidenceError, match="symlink"):
        FileLedger(link).record(_entry(1))


# ── the verifier is usable by someone who does not have this package ─────────────


def test_the_documented_ten_line_procedure_reproduces_the_verdict(tmp_path: Path) -> None:
    """`cor:third-party` in prose is worth nothing if only our code can check it.

    This is the exact procedure in the `ledger_chain` docstring, written out with no
    reference to our verifier, so the claim that a reader needs only SHA-256 and a
    JSON parser is executed rather than asserted.
    """
    path = _write(tmp_path / "ledger.jsonl", laps=4).path()

    prev = "0" * 64
    for raw in path.read_text(encoding="utf-8").splitlines():
        assert json.loads(raw)["prev"] == prev
        prev = hashlib.sha256(raw.encode("utf-8")).hexdigest()

    assert prev == verify_ledger_file(path).head


def test_verification_needs_no_agreement_about_key_order(tmp_path: Path) -> None:
    """Hashing stored bytes, not a re-serialisation, is what removes that dependency."""
    lines = ['{"prev":"' + GENESIS + '","b":1,"a":2}']
    lines.append(json.dumps({CHAIN_FIELD: line_hash(lines[0]), "z": 0, "a": 1}))
    assert chain_ledger_lines(lines).status is ChainStatus.VERIFIED


def test_a_blank_line_is_refused_because_the_writer_never_emits_one(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    _write(path, laps=2)
    text = path.read_text(encoding="utf-8").splitlines()
    path.write_text(text[0] + "\n\n" + text[1] + "\n", encoding="utf-8")
    assert verify_ledger_file(path).status is ChainStatus.BROKEN


def test_concurrent_writers_do_not_fork_the_chain(tmp_path: Path) -> None:
    """A forked chain reads as tampering, so a false alarm is the failure mode here.

    Two controllers can share a loop-level ledger. Without the lock each would read
    the same predecessor and write a sibling of it, and the verifier would then
    accuse an honest pair of writers. Every append opens its own descriptor, which
    is what `flock` arbitrates.
    """
    path = tmp_path / "ledger.jsonl"
    FileLedger(path)  # create the file once, as the controller does

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda lap: FileLedger(path).record(_entry(lap)), range(1, 41)))

    report = verify_ledger_file(path)
    assert report.status is ChainStatus.VERIFIED
    assert (report.lines, report.verified_lines) == (40, 40)


# ── the entry itself still round-trips ───────────────────────────────────────────


def test_chaining_did_not_disturb_the_entry_payload(tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path / "ledger.jsonl")
    original = _entry(7, passed=False, attempted=False)
    ledger.record(original)
    recovered = _deserialise(ledger.path().read_text(encoding="utf-8").strip())

    assert recovered.lap == original.lap
    assert recovered.verdict.passed is False
    assert recovered.attempted is False, "the field the utilisation figures come from"
