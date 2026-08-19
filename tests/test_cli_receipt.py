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
        """Uses a MULTI-row fixture with a wind-down row, so `len(entries)` and the attempt count
        differ. The single-row version of this test was vacuous: an audit showed
        `_attempts_consumed` could be replaced by `len(entries)` and it stayed green, which is the
        one substitution the whole computation exists to prevent.
        """
        entries = _entries() + [
            {
                "lap": 2, "verdict": {"passed": False, "detail": "halt"}, "decision": "halt",
                "attempted": False,   # the wind-down row: a lap on which no work was attempted
                "budget_spent": {"laps": 2, "tokens": 205, "wallclock_s": 0.05},
                "budget_declared": {"attempts": 10, "tokens": None, "wallclock_s": 990},
                "gate": {"kind": "command", "source": "shipped", "implementation": "CommandGate"},
            },
        ]
        document = receipt_document(_metadata(tmp_path), entries)
        assert len(entries) == 2
        assert document["bounds"] == {
            "attempts": {"declared": 10, "consumed": 1},   # ONE attempt across TWO rows
            "tokens": {"declared": None, "consumed": 205},
            "wallclock_s": {"declared": 990, "consumed": 0.05},
        }

    def test_the_document_is_pure(self, tmp_path, monkeypatch):
        """No clock, no filesystem. A receipt describes a run that already finished; a function
        here that read the disk or the time could report something the run never did.

        Asserting only `first == second` was vacuous — an audit noted that adding a filesystem write
        inside `receipt_document` would keep it green. It now makes the filesystem and the clock
        RAISE, so touching either is a failure rather than an unobserved side effect.
        """
        def _forbidden(*args: object, **kwargs: object) -> None:
            raise AssertionError("receipt_document touched the filesystem or the clock")

        monkeypatch.setattr(Path, "write_text", _forbidden)
        monkeypatch.setattr(Path, "read_text", _forbidden)
        monkeypatch.setattr(Path, "open", _forbidden)
        monkeypatch.setattr("time.time", _forbidden)

        first = receipt_document(_metadata(tmp_path), _entries())
        second = receipt_document(_metadata(tmp_path), _entries())
        assert first == second

    def test_the_document_says_the_ledger_is_the_authoritative_record_not_itself(self, tmp_path):
        """The artifact is written AFTER the hash chain closes and nothing hashes it. Saying so is
        the difference between a receipt and a decoration."""
        integrity = receipt_document(_metadata(tmp_path), _entries())["integrity"]
        assert integrity["authoritative_record"] == "ledger.jsonl"
        assert "NOT itself tamper-evident" in integrity["note"]
        assert str(tmp_path) in integrity["verify_command"], "must name the real run directory"

    def test_the_receipt_never_publishes_its_own_head_as_the_digest_to_verify_against(
        self, tmp_path,
    ):
        """Reverses an earlier judgement in this file, which argued a placeholder "trains a reader
        to skip instructions". That reasoning was wrong HERE: `bl verify --help` says supplying the
        head is the only check an adversary with write access to the whole run directory cannot
        satisfy, so pasting a head FROM that directory hands the adversary exactly that check. A
        runnable command that proves nothing is worse than a placeholder, because it converts a
        reader's diligence into false assurance. See the forgery test below.
        """
        integrity = receipt_document(_metadata(tmp_path), _entries())["integrity"]
        assert _HEAD not in integrity["verify_command"], (
            "the receipt pasted its own head into the command a reader is told to run"
        )
        assert "<the-digest-printed-when-the-run-ended>" in integrity["verify_command"]
        # The observed head is still published, but under a name that says where it came from.
        assert integrity["ledger_head_in_this_directory"] == _HEAD
        assert "sits in this directory" in integrity["note"]

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
        document = json.loads((run_dir / "receipt.json").read_text())
        command = document["integrity"]["verify_command"]
        parts = command.split()
        assert parts[:2] == ["bl", "verify"], command

        # The honest workflow: substitute the digest the reader kept, which for an untampered run
        # is the one the ledger actually heads at. Deliberately NOT read from the receipt — that is
        # the whole point of the placeholder.
        head = document["integrity"]["ledger_head_in_this_directory"]
        argv = [part if not part.startswith("<") else head for part in parts[1:]]
        assert main(argv) == 0, f"the receipt's published command shape does not work: {command}"
        assert "Verified" in capsys.readouterr().out

    def test_a_forged_run_directory_cannot_produce_a_green_verification(
        self, tmp_path, capsys, monkeypatch,
    ):
        """THE reason the placeholder exists. An adversary who can edit the whole run directory
        rewrites the ledger, fixes metadata's head and lap count, and regenerates the receipt. The
        result is self-consistent and false. A reader who supplies the digest THEY recorded must
        still be told no."""
        import shutil

        from bounded_loops.adapters.io.ledger_chain import head_of_lines

        monkeypatch.setenv("BOUNDED_LOOPS_TRUST_STORE", str(tmp_path / "trust"))
        loop_dir = tmp_path / "loop"
        shutil.copytree(Path("loops/assertion-density"), loop_dir)
        main([
            "run", str(loop_dir), "--run-id", "f1", "--yes",
            "--max-iterations", "1", "--gate-override", "false",
        ])
        capsys.readouterr()
        run_dir = loop_dir / ".bounded-loops" / "runs" / "f1"
        true_head = json.loads((run_dir / "metadata.json").read_text())["ledger_head"]

        rows = (run_dir / "ledger.jsonl").read_text().splitlines()
        forged = json.loads(rows[0])
        forged["prev"] = "0" * 64
        forged["verdict"] = {"passed": True, "detail": "gate passed (exit 0)", "evidence": {}}
        forged["decision"] = "done"
        line = json.dumps(forged, separators=(",", ":"))
        (run_dir / "ledger.jsonl").write_text(line + "\n")
        metadata = json.loads((run_dir / "metadata.json").read_text())
        metadata.update(
            status="DONE", reason="gate-passed", laps=1, ledger_head=head_of_lines([line]),
        )
        (run_dir / "metadata.json").write_text(json.dumps(metadata))
        main(["receipt", str(run_dir), "--write"])
        capsys.readouterr()

        # The forgery is internally consistent: the receipt now claims success.
        assert json.loads((run_dir / "receipt.json").read_text())["run"]["status"] == "DONE"
        # But the digest the reader kept does not match.
        assert main(["verify", str(run_dir), "--expect-head", true_head]) != 0
        assert "NOT VERIFIED" in capsys.readouterr().out


def test_a_failure_while_READING_the_run_also_never_fails_the_run(tmp_path, capsys):
    """The first version guarded only the write. Resolving the run directory and reading the ledger
    back sat outside the try, so a read failure propagated and broke a caller — caught by an
    unrelated test that mocks the wiring, not by anything written for this feature."""
    def _explode() -> tuple[Path, dict, list]:
        raise RuntimeError("run 'r1' does not exist")

    write_receipt_artifacts_or_warn(_explode)

    assert "could not write the receipt" in capsys.readouterr().err


class TestChangingLimits:
    """A resumed run can change its ceilings and its gate. Taking the LAST row's declaration
    reported "attempts 5/5" for a run that halted at a declared ceiling of 1 and then spent 5 —
    a blown bound rendered as a bound that held. The receipt must refuse to pick, and say so.
    """

    def _mixed(self) -> list:
        def row(lap: int, attempts: int, kind: str) -> dict:
            return {
                "lap": lap, "verdict": {"passed": False, "detail": "x"}, "decision": "continue",
                "attempted": True,
                "budget_spent": {"laps": lap, "tokens": 10, "wallclock_s": 0.1},
                "budget_declared": {"attempts": attempts, "tokens": None, "wallclock_s": 990},
                "gate": {"kind": kind, "source": "shipped"},
            }
        return [row(1, 1, "command"), row(1, 5, "command-override")]

    def test_a_run_whose_limits_changed_does_not_report_a_single_ceiling(self, tmp_path):
        document = receipt_document(_metadata(tmp_path), self._mixed())
        assert document["bounds_changed_during_run"] is True
        assert document["bounds"]["attempts"]["declared"] is None, (
            "one segment's ceiling was presented as the whole run's"
        )
        assert len(document["declarations"]) == 2

    def test_the_markdown_says_the_limits_changed_rather_than_leaving_a_gap(self, tmp_path):
        text = receipt_markdown(receipt_document(_metadata(tmp_path), self._mixed()))
        assert "limits changed while this run was in progress" in text
        assert "**changed**" in text
        assert "attempts no ceiling" not in text, (
            "a ceiling that CHANGED must not read as a ceiling that was never declared"
        )
        assert "1. attempts 1" in text and "2. attempts 5" in text

    def test_a_run_decided_by_more_than_one_gate_credits_none_of_them(self, tmp_path):
        document = receipt_document(_metadata(tmp_path), self._mixed())
        assert document["gate_changed_during_run"] is True
        assert document["gate"] == {}
        text = receipt_markdown(document)
        assert "More than one gate decided this run" in text
        assert "`command`" in text and "`command-override`" in text

    def test_an_ordinary_single_ceiling_run_is_unaffected(self, tmp_path):
        """Calibration: the refusal must be narrow. A normal run still shows its numbers."""
        document = receipt_document(_metadata(tmp_path), _entries())
        assert document["bounds_changed_during_run"] is False
        assert document["bounds"]["attempts"]["declared"] == 10
        assert document["declarations"] == []
        text = receipt_markdown(document)
        assert "| attempts | 10 | 1 |" in text
        assert "limits changed" not in text


class TestMalformedLedger:
    """A receipt that crashes on a malformed ledger fails at the exact moment its subject is
    suspect. Found by audit: `entry.get("verdict") or {}` does NOT protect against a truthy
    non-dict — a string verdict reached `.get` and raised.
    """

    _MD = {
        "run_id": "x", "status": "DONE", "reason": "r", "ledger_head": "h",
        "ledger_path": "/tmp/x/ledger.jsonl",
    }

    def _row(self, **overrides: object) -> dict:
        row = {
            "lap": 1, "verdict": {"passed": True, "detail": "d"},
            "budget_spent": {}, "budget_declared": {"attempts": 1},
        }
        row.update(overrides)
        return row

    def test_a_malformed_verdict_or_spend_does_not_crash_the_receipt(self):
        for field, value in (
            ("verdict", "not-a-dict"), ("verdict", [1]),
            ("budget_spent", [1]), ("budget_spent", "0"),
            ("gate", "not-a-dict"), ("budget_declared", "10"),
        ):
            document = receipt_document(self._MD, [self._row(**{field: value})])
            receipt_markdown(document)   # must render too, not merely build

    def test_an_unreadable_attempted_flag_counts_as_an_attempt(self):
        """Direction matters. A flag the receipt cannot read must never REDUCE reported
        consumption — a damaged or doctored ledger would then read as a cheaper run than it was.
        Only an explicit `false` excludes a row."""
        for value in (None, "no", 0, [], {}):
            document = receipt_document(self._MD, [self._row(attempted=value)])
            assert document["bounds"]["attempts"]["consumed"] == 1, (
                f"attempted={value!r} understated consumption"
            )

    def test_an_explicit_false_still_excludes_the_row(self):
        """Calibration: the wind-down row must still not count, or the whole computation is lost."""
        document = receipt_document(self._MD, [self._row(attempted=False)])
        assert document["bounds"]["attempts"]["consumed"] == 0

    def test_an_empty_ledger_renders_without_claiming_anything(self):
        document = receipt_document(self._MD, [])
        assert document["bounds"]["attempts"] == {"declared": None, "consumed": 0}
        receipt_markdown(document)


def test_the_documented_hermetic_invocation_completes_a_persisted_run(tmp_path):
    """Runs `python -m bounded_loops.cli` as a SUBPROCESS, which is the only way to catch this.

    The whole suite imports `main` from a fully-loaded module, so module-level ordering is invisible
    to it. Under `python -m`, statements execute in file order and the `if __name__ == "__main__"`
    block calls `main()` at the line it sits on — so a helper defined BELOW that block does not
    exist yet when the run reaches it.

    Found by audit: `_write_receipt_for` was appended to the end of cli.py, past that block. A run
    that reached DONE exited 1 with `NameError`, wrote no receipt, and every test still passed.
    cli.py's own comment calls this form "hermetic" and says the tests use it — they did not.
    """
    import shutil
    import subprocess
    import sys

    loop_dir = tmp_path / "loop"
    shutil.copytree(Path("loops/assertion-density"), loop_dir)
    env = {
        **__import__("os").environ,
        "BOUNDED_LOOPS_TRUST_STORE": str(tmp_path / "trust"),
    }
    result = subprocess.run(
        [sys.executable, "-m", "bounded_loops.cli", "run", str(loop_dir),
         "--run-id", "hermetic", "--yes"],
        capture_output=True, text=True, env=env, cwd=str(Path.cwd()),
    )

    assert result.returncode == 0, (
        f"a DONE run failed under `python -m`:\n{result.stderr[-1500:]}"
    )
    assert "NameError" not in result.stderr
    run_dir = loop_dir / ".bounded-loops" / "runs" / "hermetic"
    for name in RECEIPT_FILES:
        assert (run_dir / name).is_file(), f"{name} missing under the hermetic invocation"


class TestHeadlineRestsOnHashedData:
    _MD_PATH = {"ledger_path": "/tmp/x/ledger.jsonl", "ledger_head": "h", "run_id": "x"}

    def _row(self, decision: str) -> dict:
        return {
            "lap": 1, "verdict": {"passed": decision == "done", "detail": "d"},
            "decision": decision, "attempted": True,
            "budget_spent": {"laps": 1, "tokens": 5, "wallclock_s": 0.1},
            "budget_declared": {"attempts": 10, "tokens": None, "wallclock_s": 990},
        }

    def test_the_status_comes_from_the_ledger_not_from_unhashed_metadata(self):
        """`bl verify` reads metadata.json and does NOT hash it. Taking the headline from there
        meant editing that one unprotected file flipped the receipt from HALT to DONE while
        verification stayed green."""
        document = receipt_document({**self._MD_PATH, "status": "DONE"}, [self._row("halt")])
        assert document["run"]["status"] == "HALT", "the headline followed the unhashed file"
        assert document["run"]["status_disagrees_with_metadata"] is True

    def test_a_disagreement_is_stated_in_the_rendered_receipt(self):
        text = receipt_markdown(
            receipt_document({**self._MD_PATH, "status": "DONE"}, [self._row("halt")])
        )
        assert "disagrees with the summary filed beside it" in text
        assert "Treat this run as suspect" in text

    def test_agreement_is_silent(self):
        """Calibration: the warning must not fire on every ordinary run."""
        text = receipt_markdown(
            receipt_document({**self._MD_PATH, "status": "HALT"}, [self._row("halt")])
        )
        assert "disagrees" not in text

    def test_a_ledger_that_never_reached_a_terminal_row_says_so(self):
        document = receipt_document({**self._MD_PATH, "status": "DONE"}, [self._row("continue")])
        assert document["run"]["status"] == "INCOMPLETE"


def test_an_old_ledger_reports_its_bounds_as_not_recorded_not_as_unbounded():
    """A row written before declared bounds existed carries no declaration. Rendering that as
    "no ceiling" asserts the run was UNBOUNDED — which the data does not say and which is very
    likely false, since the loop had a bounds.yaml all along. An absent record and a declared
    absence are different facts."""
    old_row = [{
        "lap": 1, "verdict": {"passed": True, "detail": "ok"}, "decision": "done",
        "attempted": True, "budget_spent": {"laps": 1, "tokens": 5, "wallclock_s": 0.1},
    }]
    document = receipt_document(
        {"run_id": "x", "status": "DONE", "ledger_head": "h",
         "ledger_path": "/tmp/x/ledger.jsonl"}, old_row,
    )
    assert document["bounds_recorded"] is False
    text = receipt_markdown(document)
    assert "not recorded" in text
    assert "no ceiling" not in text, "an unrecorded ceiling was asserted to be absent"
