"""`bl verify` must catch a real edit to a real run, and must refuse to bless a guess.

SEC-01, second half. The chain is worth nothing to a reader who has no way to check
it, and `cor:third-party` in the paper claims a reader can. These tests drive the
shipped command against receipts produced by the shipped controller.

The hardest case is deliberately included: a rewriter who recomputes every link
defeats the chain, and the only thing that catches them is a head recorded outside
the file. If `--expect-head` did not catch that, the command would be theatre.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bounded_loops.adapters.io.file_ledger import FileLedger
from bounded_loops.adapters.io.ledger_chain import GENESIS, line_hash
from bounded_loops.cli import main
from bounded_loops.domain.models import LedgerEntry, Verdict


def _record(path: Path, laps: int = 3, *, final_passed: bool = True) -> FileLedger:
    ledger = FileLedger(path)
    for lap in range(1, laps + 1):
        last = lap == laps
        ledger.record(
            LedgerEntry(
                lap=lap,
                ts=f"2026-08-17T00:00:{lap:02d}Z",
                verdict=Verdict(
                    passed=last and final_passed, detail=f"lap {lap}", evidence={},
                ),
                decision=("done" if final_passed else "halt") if last else "continue",
                budget_spent={"laps": lap},
            )
        )
    return ledger


def _run_dir(
    tmp_path: Path, *, laps: int = 3, head: str | None = None, final_passed: bool = True,
) -> Path:
    directory = tmp_path / "run-1"
    directory.mkdir()
    ledger = _record(directory / "ledger.jsonl", laps=laps, final_passed=final_passed)
    (directory / "metadata.json").write_text(
        json.dumps({
            "run_id": "run-1",
            "status": "DONE" if final_passed else "HALT",
            "reason": "gate-passed" if final_passed else "max-iterations",
            "laps": laps,
            "ledger_head": ledger.head() if head is None else head,
        }),
        encoding="utf-8",
    )
    return directory


def _rewrite_line(path: Path, index: int, row: dict) -> None:
    """Replace one line and refuse to proceed if the bytes did not change.

    Two tamper tests in this file initially passed while editing nothing: a
    `.replace()` that missed because of a space, and a re-serialisation of a field
    that already held the value being 'forged'. A tamper test that does not tamper
    asserts nothing, and both looked green. The guard is here so the third one
    cannot.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    forged = json.dumps(row, separators=(",", ":"))
    assert forged != lines[index], "the edit changed no bytes, so it proves nothing"
    lines[index] = forged
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _verify(*argv: str) -> tuple[int, dict]:
    import io
    import contextlib

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = main(["verify", *argv, "--json"])
    return code, json.loads(buffer.getvalue())


def test_an_intact_run_verifies_and_exits_zero(tmp_path: Path) -> None:
    code, payload = _verify(str(_run_dir(tmp_path)))
    assert code == 0
    assert payload["verified"] is True
    assert {check["check"] for check in payload["checks"]} == {
        "chain", "anchor", "completeness",
    }
    assert all(check["passed"] for check in payload["checks"])


def test_an_edited_row_fails_the_chain_check(tmp_path: Path) -> None:
    directory = _run_dir(tmp_path)
    ledger = directory / "ledger.jsonl"
    forged = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
    forged["verdict"]["passed"] = True
    _rewrite_line(ledger, 0, forged)

    code, payload = _verify(str(directory))
    assert code == 1
    chain = next(c for c in payload["checks"] if c["check"] == "chain")
    assert chain["passed"] is False and chain["status"] == "BROKEN"


def test_a_removed_tail_fails_completeness_even_though_the_chain_is_intact(
    tmp_path: Path,
) -> None:
    """The hypothesis hashing alone cannot supply: a truncated log is well formed."""
    directory = _run_dir(tmp_path, laps=4)
    ledger = directory / "ledger.jsonl"
    lines = ledger.read_text(encoding="utf-8").splitlines()
    ledger.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")

    code, payload = _verify(str(directory))
    assert code == 1
    chain = next(c for c in payload["checks"] if c["check"] == "chain")
    completeness = next(c for c in payload["checks"] if c["check"] == "completeness")
    assert chain["passed"] is True, "the prefix is still a valid chain — that is the point"
    assert completeness["passed"] is False and completeness["status"] == "TRUNCATED"


def test_a_full_rewrite_is_caught_only_by_the_recorded_head(tmp_path: Path) -> None:
    """The adversary the construction cannot beat, and the witness that can."""
    directory = _run_dir(tmp_path, laps=3)
    ledger = directory / "ledger.jsonl"
    witnessed = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    original_head = witnessed["ledger_head"]

    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    rows[0]["verdict"]["passed"] = True
    rebuilt: list[str] = []
    prev = GENESIS
    for row in rows:
        row["prev"] = prev
        line = json.dumps(row, separators=(",", ":"))
        rebuilt.append(line)
        prev = line_hash(line)
    ledger.write_text("\n".join(rebuilt) + "\n", encoding="utf-8")

    # The adversary also rewrites the receipt, because it sits on the same disk.
    witnessed["ledger_head"] = prev
    (directory / "metadata.json").write_text(json.dumps(witnessed), encoding="utf-8")

    code, payload = _verify(str(directory))
    assert code == 0, "self-consistent again: this is the documented limit"

    code, payload = _verify(str(directory), "--expect-head", original_head)
    assert code == 1
    anchor = next(c for c in payload["checks"] if c["check"] == "anchor")
    assert anchor["status"] == "MISMATCH"
    assert original_head in anchor["detail"]


def test_editing_the_final_row_is_caught_by_the_anchor_and_not_by_the_chain(
    tmp_path: Path,
) -> None:
    """The row an adversary most wants is the one the chain cannot protect.

    `thm:chain` detects modification of any r_j with j < n — the bound excludes the
    last row, because no successor exists to carry its hash. The last row is where
    the terminal verdict lives, so it is the single most attractive target, and the
    recorded head is its only defence. Found by editing a real receipt rather than by
    reading the proof: the chain check passed and the anchor check was what failed.
    """
    directory = _run_dir(tmp_path, laps=3, final_passed=False)
    ledger = directory / "ledger.jsonl"
    forged = json.loads(ledger.read_text(encoding="utf-8").splitlines()[-1])
    forged["verdict"]["passed"] = True
    forged["decision"] = "done"
    _rewrite_line(ledger, 2, forged)

    code, payload = _verify(str(directory))
    checks = {check["check"]: check for check in payload["checks"]}
    assert checks["chain"]["passed"] is True, "no successor exists to break"
    assert checks["anchor"]["passed"] is False
    assert code == 1


def test_a_ledger_with_no_receipt_does_not_report_success(tmp_path: Path) -> None:
    """A verifier that exits 0 on 'could not tell' is worse than no verifier."""
    path = tmp_path / "ledger.jsonl"
    _record(path, laps=2)

    code, payload = _verify(str(path))
    assert code == 1
    statuses = {check["check"]: check["status"] for check in payload["checks"]}
    assert statuses["chain"] == "VERIFIED"
    assert statuses["anchor"] == "NO_WITNESS"
    assert statuses["completeness"] == "NO_RECEIPT"


def test_supplying_the_head_by_hand_is_enough_without_a_receipt(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    head = _record(path, laps=2).head()
    code, payload = _verify(str(path), "--expect-head", head)
    assert code == 1, "the lap accounting still has nothing to check against"
    anchor = next(c for c in payload["checks"] if c["check"] == "anchor")
    assert anchor["passed"] is True
    assert anchor["status"] == "MATCH_EXTERNAL"
    assert anchor["witness"] == "external"


def test_a_pre_chain_ledger_is_reported_as_unverifiable_not_as_verified(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "legacy"
    directory.mkdir()
    (directory / "ledger.jsonl").write_text(
        json.dumps({"lap": 1, "ts": "t", "verdict": {"passed": True, "detail": "",
                    "evidence": {}}, "decision": "done", "budget_spent": {}}) + "\n",
        encoding="utf-8",
    )
    (directory / "metadata.json").write_text(
        json.dumps({"run_id": "legacy", "status": "DONE", "reason": "gate-passed",
                    "laps": 1}),
        encoding="utf-8",
    )
    code, payload = _verify(str(directory))
    assert code == 1
    chain = next(c for c in payload["checks"] if c["check"] == "chain")
    assert chain["status"] == "UNCHAINED"


def test_human_output_names_the_head_so_it_can_be_copied(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = _run_dir(tmp_path)
    expected = json.loads(
        (directory / "metadata.json").read_text(encoding="utf-8")
    )["ledger_head"]

    assert main(["verify", str(directory)]) == 0
    printed = capsys.readouterr().out
    assert expected in printed
    assert "Verified:" in printed


def test_a_colocated_witness_is_not_reported_as_the_strong_check(tmp_path: Path) -> None:
    """An enterprise review caught this: both witnesses printed the same green tick.

    The receipt lives in the directory it vouches for, so anyone who edits the ledger
    edits the receipt in the same pass. The check still passes — it catches the careless
    editor, which is most of them — but the output must not let a reader infer that an
    external party confirmed anything.
    """
    directory = _run_dir(tmp_path)
    code, payload = _verify(str(directory))
    anchor = next(c for c in payload["checks"] if c["check"] == "anchor")

    assert code == 0 and anchor["passed"] is True
    assert anchor["status"] == "MATCH_COLOCATED"
    assert anchor["witness"] == "co-located"
    assert "--expect-head" in anchor["detail"], "and it says how to get the strong check"


def test_the_human_summary_distinguishes_the_two_witnesses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = _run_dir(tmp_path)
    head = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))["ledger_head"]

    assert main(["verify", str(directory)]) == 0
    weak = capsys.readouterr().out
    assert "Not established" in weak, "a co-located pass must state what it did not show"

    assert main(["verify", str(directory), "--expect-head", head]) == 0
    strong = capsys.readouterr().out
    assert "outside the run directory" in strong
    assert "Not established" not in strong


def test_a_mistyped_path_is_not_accused_of_tampering(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """BROKEN covers three unrelated situations; only one of them is an accusation."""
    assert main(["verify", str(tmp_path / "typo")]) == 1
    printed = capsys.readouterr().out
    assert "no ledger at this path" in printed
    assert "edited" not in printed, "a path that never existed cannot have been edited"


def test_a_symlinked_ledger_says_so_rather_than_accusing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    real = tmp_path / "real.jsonl"
    _record(real, laps=1)
    link = tmp_path / "link.jsonl"
    link.symlink_to(real)

    assert main(["verify", str(link)]) == 1
    printed = capsys.readouterr().out
    assert "symlink" in printed
    assert "edited after it was written" not in printed
