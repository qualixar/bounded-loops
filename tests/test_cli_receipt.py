"""The portable receipt artifact.

`bl runs --show` prints to a terminal, which cannot be attached to a paper, a pull request or a
compliance ticket. These cover the written file: what it claims, what it refuses to claim, and that
the instruction it prints for checking itself actually works.
"""
from __future__ import annotations

import json
from pathlib import Path

from bounded_loops.cli import main
from bounded_loops.cli_receipt import (
    RECEIPT_FILES,
    receipt_document,
    receipt_markdown,
    write_receipt_artifacts,
    write_receipt_artifacts_or_warn,
)

_HEAD = "d4" + "5" * 62


def _metadata(run_dir: Path, **overrides: object) -> dict:
    base = {
        "run_id": "r1", "status": "DONE", "reason": "gate-passed", "laps": 1,
        "workspace": str(run_dir / "workspace"),
        "ledger_path": str(run_dir / "ledger.jsonl"),
        "ledger_head": _HEAD,
    }
    base.update(overrides)
    return base


def _entries(**overrides: object) -> list:
    entry = {
        "lap": 1, "verdict": {"passed": True, "detail": "gate passed (exit 0)"},
        "decision": "done", "attempted": True,
        "budget_spent": {"laps": 1, "tokens": 205, "wallclock_s": 0.05},
        "budget_declared": {"attempts": 10, "tokens": None, "wallclock_s": 990},
        "gate": {"kind": "command", "source": "shipped", "implementation": "CommandGate"},
    }
    entry.update(overrides)
    return [entry]


class TestDocument:
    def test_the_document_pairs_every_declared_ceiling_with_its_consumption(self, tmp_path):
        document = receipt_document(_metadata(tmp_path), _entries())
        assert document["bounds"] == {
            "attempts": {"declared": 10, "consumed": 1},
            "tokens": {"declared": None, "consumed": 205},
            "wallclock_s": {"declared": 990, "consumed": 0.05},
        }

    def test_the_document_is_pure(self, tmp_path):
        """No clock, no filesystem. A receipt describes a run that already finished; a function
        here that read the disk or the time could report something the run never did."""
        first = receipt_document(_metadata(tmp_path), _entries())
        second = receipt_document(_metadata(tmp_path), _entries())
        assert first == second

    def test_the_document_says_the_ledger_is_the_authoritative_record_not_itself(self, tmp_path):
        """The artifact is written AFTER the hash chain closes and nothing hashes it. Saying so is
        the difference between a receipt and a decoration."""
        integrity = receipt_document(_metadata(tmp_path), _entries())["integrity"]
        assert integrity["authoritative_record"] == "ledger.jsonl"
        assert "NOT itself tamper-evident" in integrity["note"]
        assert _HEAD in integrity["verify_command"]
        assert str(tmp_path) in integrity["verify_command"], (
            "the verify command must name the real run directory, not a placeholder nobody can paste"
        )

    def test_a_run_with_no_recorded_head_offers_no_verify_command(self, tmp_path):
        """Calibration: an empty string, not a command that silently omits --expect-head. A verify
        invocation without the head checks the chain against itself, which an adversary with write
        access to the directory can satisfy."""
        document = receipt_document(_metadata(tmp_path, ledger_head=""), _entries())
        assert document["integrity"]["verify_command"] == ""


class TestMarkdown:
    def test_markdown_renders_from_the_document_so_the_two_files_cannot_drift(self, tmp_path):
        document = receipt_document(_metadata(tmp_path), _entries())
        text = receipt_markdown(document)
        assert "| attempts | 10 | 1 |" in text
        assert "| tokens | no ceiling | 205 |" in text

    def test_a_shipped_gate_is_not_described_as_coming_from_shipped(self, tmp_path):
        """A shipped gate has no separate distribution. The first version rendered
        "shipped, from `shipped`" — a tautology in the one sentence a reader consults to find out
        where their gate came from."""
        text = receipt_markdown(receipt_document(_metadata(tmp_path), _entries()))
        assert "shipped, from" not in text
        assert "Gate `command` — shipped, implemented by `CommandGate`" in text

    def test_a_third_party_gate_names_its_distribution(self, tmp_path):
        entries = _entries(gate={
            "kind": "acme-check", "source": "plugin",
            "distribution": "acme-gates", "implementation": "AcmeGate",
        })
        text = receipt_markdown(receipt_document(_metadata(tmp_path), entries))
        assert "from `acme-gates`" in text

    def test_a_run_without_recorded_provenance_says_so_rather_than_inventing_a_gate(self, tmp_path):
        entries = _entries()
        del entries[0]["gate"]
        text = receipt_markdown(receipt_document(_metadata(tmp_path), entries))
        assert "Not recorded" in text
        assert "None" not in text

    def test_a_detail_containing_a_pipe_does_not_break_the_table(self, tmp_path):
        entries = _entries(verdict={"passed": False, "detail": "a | b\nsecond line"})
        text = receipt_markdown(receipt_document(_metadata(tmp_path), entries))
        lap_row = [line for line in text.splitlines() if line.startswith("| 1 |")][0]
        # Count only STRUCTURAL pipes — an escaped `\|` still contains the character, so a naive
        # count of "|" reports the escaping as a failure. Five columns means six cell borders.
        structural = sum(
            1 for index, character in enumerate(lap_row)
            if character == "|" and (index == 0 or lap_row[index - 1] != "\\")
        )
        assert structural == 6, f"the pipe escaped into the table structure: {lap_row}"
        assert "\\|" in lap_row, "the pipe was not escaped at all"
        assert "second line" in lap_row, "the newline was dropped instead of flattened"


class TestWriting:
    def test_writing_produces_both_artifacts(self, tmp_path):
        written = write_receipt_artifacts(tmp_path, _metadata(tmp_path), _entries())
        assert {path.name for path in written} == set(RECEIPT_FILES)
        assert json.loads((tmp_path / "receipt.json").read_text())["run"]["status"] == "DONE"
        assert (tmp_path / "receipt.md").read_text().startswith("# Run receipt")

    def test_a_failure_to_write_the_artifact_never_fails_the_run(self, tmp_path, capsys, monkeypatch):
        """The ledger is already written and already authoritative. A read-only volume or a full
        disk must not turn a run that reached DONE into a failure — but the absence must be visible
        rather than silent."""
        import bounded_loops.cli_receipt as receipt_module

        def _explode(*args: object, **kwargs: object) -> None:
            raise OSError("read-only file system")

        monkeypatch.setattr(receipt_module, "_write_atomically", _explode)
        write_receipt_artifacts_or_warn(lambda: (tmp_path, _metadata(tmp_path), _entries()))

        assert not (tmp_path / "receipt.md").exists()
        assert "could not write the receipt" in capsys.readouterr().err


class TestCommand:
    def _run_dir(self, tmp_path) -> Path:
        run_dir = tmp_path / "runs" / "r1"
        run_dir.mkdir(parents=True)
        (run_dir / "metadata.json").write_text(json.dumps(_metadata(run_dir)), encoding="utf-8")
        (run_dir / "ledger.jsonl").write_text(
            json.dumps(_entries()[0]) + "\n", encoding="utf-8")
        return run_dir

    def test_receipt_renders_markdown_by_default(self, tmp_path, capsys):
        assert main(["receipt", str(self._run_dir(tmp_path))]) == 0
        assert "# Run receipt" in capsys.readouterr().out

    def test_receipt_json_is_machine_readable(self, tmp_path, capsys):
        assert main(["receipt", str(self._run_dir(tmp_path)), "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["bounds"]["attempts"]["declared"] == 10

    def test_receipt_write_puts_both_files_in_the_run_directory(self, tmp_path, capsys):
        run_dir = self._run_dir(tmp_path)
        assert main(["receipt", str(run_dir), "--write"]) == 0
        capsys.readouterr()
        for name in RECEIPT_FILES:
            assert (run_dir / name).is_file()

    def test_receipt_refuses_a_directory_that_is_not_a_run(self, tmp_path, capsys):
        assert main(["receipt", str(tmp_path)]) == 2
        assert "not a readable run directory" in capsys.readouterr().err


class TestEndToEnd:
    def test_a_persisted_run_writes_its_own_receipt_without_being_asked(
        self, tmp_path, monkeypatch,
    ):
        """A receipt you have to ask for is one nobody has when they need it."""
        import shutil

        monkeypatch.setenv("BOUNDED_LOOPS_TRUST_STORE", str(tmp_path / "trust"))
        loop_dir = tmp_path / "loop"
        shutil.copytree(Path("loops/assertion-density"), loop_dir)

        assert main(["run", str(loop_dir), "--run-id", "e1", "--yes"]) == 0

        run_dir = loop_dir / ".bounded-loops" / "runs" / "e1"
        for name in RECEIPT_FILES:
            assert (run_dir / name).is_file(), f"the run did not write {name}"
        document = json.loads((run_dir / "receipt.json").read_text())
        assert document["bounds"]["attempts"] == {"declared": 10, "consumed": 1}
        assert document["gate"]["kind"] == "command"

    def test_the_verify_command_the_receipt_prints_actually_verifies_the_run(
        self, tmp_path, capsys, monkeypatch,
    ):
        """THE credibility test. The artifact tells a reader how to check it; if that instruction
        does not work, the artifact is worse than silent — it is a false assurance."""
        import shutil

        monkeypatch.setenv("BOUNDED_LOOPS_TRUST_STORE", str(tmp_path / "trust"))
        loop_dir = tmp_path / "loop"
        shutil.copytree(Path("loops/assertion-density"), loop_dir)
        assert main(["run", str(loop_dir), "--run-id", "v1", "--yes"]) == 0
        capsys.readouterr()

        run_dir = loop_dir / ".bounded-loops" / "runs" / "v1"
        command = json.loads((run_dir / "receipt.json").read_text())["integrity"]["verify_command"]
        parts = command.split()
        assert parts[:2] == ["bl", "verify"], command

        # Run exactly the arguments the receipt published.
        assert main(parts[1:]) == 0, f"the receipt's own verify command failed: {command}"
        assert "Verified" in capsys.readouterr().out


def test_a_failure_while_READING_the_run_also_never_fails_the_run(tmp_path, capsys):
    """The first version guarded only the write. Resolving the run directory and reading the ledger
    back sat outside the try, so a read failure propagated and broke a caller — caught by an
    unrelated test that mocks the wiring, not by anything written for this feature."""
    def _explode() -> tuple[Path, dict, list]:
        raise RuntimeError("run 'r1' does not exist")

    write_receipt_artifacts_or_warn(_explode)

    assert "could not write the receipt" in capsys.readouterr().err
