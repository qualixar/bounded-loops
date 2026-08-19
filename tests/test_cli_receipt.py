"""The portable receipt artifact.

`bl runs --show` prints to a terminal, which cannot be attached to a paper, a pull request or a
compliance ticket. These cover the written file: what it claims, what it refuses to claim, and that
the instruction it prints for checking itself actually works.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from bounded_loops.cli import main
# The builders and the writer live in `application.receipt` so that every path which persists a run
# can write the artifact; `cli_receipt` is the presentation half.
from bounded_loops.application.receipt import (
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
        # An audit noted the clock was only half-blocked: the budget meter uses `time.monotonic`,
        # and a timestamp would come from `datetime`, so patching `time.time` alone left the two
        # clocks this codebase actually reads wide open.
        monkeypatch.setattr("time.monotonic", _forbidden)
        # No `datetime` patch here, deliberately. `datetime.datetime` is an immutable C type, so
        # `setattr` on it raises TypeError — the attempt to patch it broke this test rather than
        # strengthening it. The clock property is enforced structurally instead, by
        # `test_the_receipt_module_imports_no_clock` below: a module that never imports a clock
        # cannot read one, which is a stronger guarantee than any monkeypatch and cannot go stale.

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
        import bounded_loops.application.receipt as receipt_module

        def _explode(*args: object, **kwargs: object) -> None:
            raise OSError("read-only file system")

        monkeypatch.setattr(receipt_module, "_write_atomically", _explode)
        write_receipt_artifacts_or_warn(tmp_path, lambda: (_metadata(tmp_path), _entries()))

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
        run_output = capsys.readouterr().out

        run_dir = loop_dir / ".bounded-loops" / "runs" / "v1"
        document = json.loads((run_dir / "receipt.json").read_text())
        command = document["integrity"]["verify_command"]
        parts = command.split()
        assert parts[:2] == ["bl", "verify"], command

        # The honest workflow: the digest the operator kept, taken from the RUN'S OWN OUTPUT rather
        # than from the run directory. An audit pointed out that reading it back from receipt.json —
        # as this test first did — models exactly the misuse the placeholder exists to prevent, so
        # the test would have "verified" the false green rather than the real workflow.
        head = re.search(r"Ledger head: ([0-9a-f]{64})", run_output).group(1)
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
            # A thorough adversary also fixes the row count. Leaving it stale made the forgery
            # detectable by the completeness witness alone, which meant this test was no longer
            # exercising the thing it is about — the kept-digest check. Narrowing the attack is
            # progress; letting the test pass for the easier reason is not.
            ledger_rows=1,
        )
        (run_dir / "metadata.json").write_text(json.dumps(metadata))
        main(["receipt", str(run_dir), "--write"])
        capsys.readouterr()

        # The forgery is internally consistent: the receipt now claims success.
        assert json.loads((run_dir / "receipt.json").read_text())["run"]["status"] == "DONE"
        # And it survives every check that does NOT require an outside digest.
        assert main(["verify", str(run_dir)]) == 0
        capsys.readouterr()
        # But the digest the reader kept does not match.
        assert main(["verify", str(run_dir), "--expect-head", true_head]) != 0
        assert "NOT VERIFIED" in capsys.readouterr().out

        # POSITIVE CONTROL. Without this the test passes if verification simply always fails —
        # an audit called the two assertions above "complicit": either alone stays green. The
        # forged head must still verify against the forged ledger, so what the test detects is
        # specifically the disagreement with the digest the reader kept, not a broken verifier.
        forged_head = json.loads((run_dir / "metadata.json").read_text())["ledger_head"]
        assert forged_head != true_head
        assert main(["verify", str(run_dir), "--expect-head", forged_head]) == 0
        assert "Verified" in capsys.readouterr().out


def test_a_failure_while_READING_the_run_also_never_fails_the_run(tmp_path, capsys):
    """The first version guarded only the write. Resolving the run directory and reading the ledger
    back sat outside the try, so a read failure propagated and broke a caller — caught by an
    unrelated test that mocks the wiring, not by anything written for this feature."""
    def _explode() -> tuple[dict, list]:
        raise RuntimeError("run 'r1' does not exist")

    write_receipt_artifacts_or_warn(tmp_path, _explode)

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


class TestWorkCeiling:
    """`max_wallclock_s` is the operator's number and the honest TOTAL — the wind-down turn is
    partitioned out of it, never added to it, so a run cannot outlive its declared ceiling. But WORK
    stops earlier, at total minus the effective reserve. Quoting only the total answers "what was
    declared?" and hides "when does this get stopped?". Audit finding; both numbers now appear.
    """

    _MD = {"run_id": "x", "status": "DONE", "ledger_head": "h",
           "ledger_path": "/tmp/x/ledger.jsonl"}

    def _row(self, **declared: object) -> list:
        base = {"attempts": 10, "tokens": None, "wallclock_s": 990, "wallclock_work_s": 900.0}
        base.update(declared)
        return [{
            "lap": 1, "verdict": {"passed": True, "detail": "ok"}, "decision": "done",
            "attempted": True,
            "budget_spent": {"laps": 1, "tokens": 5, "wallclock_s": 0.1},
            "budget_declared": base,
        }]

    def test_the_receipt_names_where_work_is_cut_off_not_only_the_total(self):
        text = receipt_markdown(receipt_document(self._MD, self._row()))
        assert "| wall clock | 990s |" in text, "the operator's declared total must still be shown"
        assert "Work stops at **900.0s**" in text

    def test_no_second_number_when_nothing_is_held_back(self):
        """Calibration: with a zero reserve the work ceiling IS the total, and repeating it would be
        noise that trains a reader to skip the line."""
        text = receipt_markdown(receipt_document(self._MD, self._row(wallclock_work_s=990)))
        assert "Work stops at" not in text

    def test_no_second_number_when_no_wallclock_ceiling_was_declared(self):
        text = receipt_markdown(
            receipt_document(self._MD, self._row(wallclock_s=None, wallclock_work_s=None))
        )
        assert "Work stops at" not in text
        assert "| wall clock | no ceiling |" in text

    def test_a_real_run_records_the_work_ceiling(self, tmp_path, monkeypatch):
        """End to end: `composition` must derive it from the SAME function the budget meter uses,
        or the receipt would quote a cutoff the engine does not enforce."""
        import shutil

        from bounded_loops.adapters.io.budget import effective_reserve_s
        from bounded_loops.application.manifest import load as load_manifest

        monkeypatch.setenv("BOUNDED_LOOPS_TRUST_STORE", str(tmp_path / "trust"))
        loop_dir = tmp_path / "loop"
        shutil.copytree(Path("loops/assertion-density"), loop_dir)
        assert main(["run", str(loop_dir), "--run-id", "wc", "--yes"]) == 0

        bounds = load_manifest(loop_dir).bounds
        expected = round(bounds.max_wallclock_s - effective_reserve_s(bounds), 2)
        row = json.loads(
            (loop_dir / ".bounded-loops/runs/wc/ledger.jsonl").read_text().splitlines()[0]
        )
        assert row["budget_declared"]["wallclock_work_s"] == expected
        assert row["budget_declared"]["wallclock_s"] == bounds.max_wallclock_s


class TestReasonAlsoRestsOnHashedData:
    """Round 1 moved the STATUS off metadata.json and left the REASON behind, so half the headline
    still rested on the one file `bl verify` reads and does not hash: a verified HALT could keep its
    status and have the clause after the dash rewritten to anything. Fixing one site and leaving its
    sibling is this codebase's most-repeated mistake — committed here inside the fix for it.
    """

    _MD_PATH = {"ledger_path": "/tmp/x/ledger.jsonl", "ledger_head": "h", "run_id": "x"}

    def _rows(self) -> list:
        return [{
            "lap": 3, "verdict": {"passed": False, "detail": "max_iterations 2 reached at lap 3"},
            "decision": "halt", "attempted": False,
            "budget_spent": {"laps": 3, "tokens": 20, "wallclock_s": 0.3},
            "budget_declared": {"attempts": 2, "tokens": None, "wallclock_s": 990},
        }]

    def test_the_reason_comes_from_the_ledger_not_from_metadata(self):
        document = receipt_document(
            {**self._MD_PATH, "status": "HALT", "reason": "gate-passed, all good"}, self._rows(),
        )
        assert document["run"]["reason"] == "max_iterations 2 reached at lap 3"
        assert document["run"]["reason_in_metadata"] == "gate-passed, all good"
        # NOT flagged, and that is deliberate: a reason-only mismatch is not evidence of tampering,
        # because for DONE and PAUSE the two strings legitimately differ (the engine's canonical
        # label vs the gate's own sentence). This assertion used to require a flag here, which made
        # every honest successful run accuse itself. The protection that matters is above: the
        # receipt DISPLAYS the ledger's reason, so metadata's copy is not load-bearing.
        assert document["run"]["status_disagrees_with_metadata"] is False

    def test_a_rewritten_reason_is_simply_not_the_one_displayed(self):
        """The premise of this test was wrong when first written: it required a reason-only mismatch
        to raise an accusation, which fires on every honest DONE run. The real protection is that the
        rewritten string never reaches the reader at all."""
        text = receipt_markdown(receipt_document(
            {**self._MD_PATH, "status": "HALT", "reason": "gate-passed, all good"}, self._rows(),
        ))
        assert "max_iterations 2 reached at lap 3" in text, "the ledger's reason must be shown"
        assert "gate-passed, all good" not in text, "the unhashed reason reached the reader"
        assert "Treat this run as suspect" not in text

    def test_an_honest_run_reports_no_disagreement(self):
        """Calibration: status AND reason both matching must stay silent."""
        document = receipt_document(
            {**self._MD_PATH, "status": "HALT", "reason": "max_iterations 2 reached at lap 3"},
            self._rows(),
        )
        assert document["run"]["status_disagrees_with_metadata"] is False


def test_a_ledger_mixing_declared_and_undeclared_rows_counts_as_changed():
    """Skipping rows that lack the field meant a ledger whose early rows predate declared bounds and
    whose later rows carry them read as UNIFORM — and the whole run was reported as having run under
    the later declaration, including the segment that declared nothing."""
    rows = [
        {"lap": 1, "verdict": {"passed": False, "detail": "x"}, "decision": "continue",
         "attempted": True, "budget_spent": {"laps": 1, "tokens": 5, "wallclock_s": 0.1}},
        {"lap": 2, "verdict": {"passed": True, "detail": "y"}, "decision": "done",
         "attempted": True, "budget_spent": {"laps": 2, "tokens": 9, "wallclock_s": 0.2},
         "budget_declared": {"attempts": 9, "tokens": None, "wallclock_s": 990}},
    ]
    document = receipt_document(
        {"run_id": "x", "status": "DONE", "ledger_head": "h",
         "ledger_path": "/tmp/x/ledger.jsonl"}, rows,
    )
    assert document["bounds_changed_during_run"] is True
    assert document["bounds"]["attempts"]["declared"] is None, (
        "one segment's declaration was presented as the whole run's"
    )
    assert "limits changed while this run was in progress" in receipt_markdown(document)


class TestResumedRunSpend:
    """`--resume` builds a FRESH BudgetMeter and restarts the lap counter while the ledger stays
    append-only, so the last row's spend describes only the final segment. Reading it as the run's
    total under-reported every earlier segment — while attempts were already counted across the whole
    ledger, so one table held two different intervals. Both auditors called this a blocker: a cost
    figure that SHRINKS when a run is resumed is worse than no cost figure.
    """

    _MD = {"run_id": "x", "status": "DONE", "ledger_head": "h",
           "ledger_path": "/tmp/x/ledger.jsonl"}

    def _segment(self, laps: int, tokens_each: int) -> list:
        return [
            {
                "lap": lap, "verdict": {"passed": False, "detail": "x"}, "decision": "continue",
                "attempted": True,
                # cumulative WITHIN a segment, which is what the meter reports
                "budget_spent": {"laps": lap, "tokens": tokens_each * lap,
                                 "wallclock_s": round(0.1 * lap, 2)},
                "budget_declared": {"attempts": 4, "tokens": None, "wallclock_s": 990,
                                    "wallclock_work_s": 900.0},
            }
            for lap in range(1, laps + 1)
        ]

    def test_spend_is_the_sum_of_the_segments_not_the_last_one(self):
        entries = self._segment(2, 100) + self._segment(2, 100)   # 200 + 200
        document = receipt_document(self._MD, entries)
        assert document["segments"] == 2
        assert document["bounds"]["tokens"]["consumed"] == 400, (
            "the receipt reported one segment's spend as the whole run's"
        )
        assert document["bounds"]["attempts"]["consumed"] == 4, "attempts must cover the same span"

    def test_an_unresumed_run_is_unchanged(self):
        """Calibration: the sum must equal the last row when there is only one segment."""
        document = receipt_document(self._MD, self._segment(3, 100))
        assert document["segments"] == 1
        assert document["bounds"]["tokens"]["consumed"] == 300

    def test_a_real_resumed_run_reports_the_total(self, tmp_path, capsys, monkeypatch):
        """End to end, because the segment seam is a property of what the ENGINE writes: only a real
        resume restarts the lap counter and the meter."""
        import shutil

        monkeypatch.setenv("BOUNDED_LOOPS_TRUST_STORE", str(tmp_path / "trust"))
        loop_dir = tmp_path / "loop"
        shutil.copytree(Path("loops/assertion-density"), loop_dir)
        args = ["--yes", "--max-iterations", "1", "--gate-override", "false"]
        main(["run", str(loop_dir), "--run-id", "r", *args])
        main(["run", str(loop_dir), "--run-id", "r", "--resume", *args])
        capsys.readouterr()

        document = json.loads(
            (loop_dir / ".bounded-loops/runs/r/receipt.json").read_text()
        )
        assert document["segments"] == 2
        rows = [
            json.loads(line) for line in
            (loop_dir / ".bounded-loops/runs/r/ledger.jsonl").read_text().splitlines() if line.strip()
        ]
        last_only = rows[-1]["budget_spent"]["tokens"]
        assert document["bounds"]["tokens"]["consumed"] > last_only, (
            f"a resumed run still reports one segment's spend ({last_only})"
        )


def test_every_persist_path_writes_the_receipt_because_they_share_one(tmp_path):
    """Three code paths persist a run — the CLI, the MCP server and the graph loop bridge — and only
    the CLI wrote a receipt, so the others left a STALE one beside a newer ledger while `bl verify`
    reported the run intact. The write now lives where the metadata write already lives, so a future
    fourth path cannot forget it. This test calls `write_run_metadata` DIRECTLY, i.e. as those other
    paths do, without going near the CLI.
    """
    import shutil

    from bounded_loops.application.run_store import run_dir, write_run_metadata
    from bounded_loops.domain.models import Outcome, Status

    loop_dir = tmp_path / "loop"
    shutil.copytree(Path("loops/assertion-density"), loop_dir)
    directory = run_dir(loop_dir, "viamcp")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "ledger.jsonl").write_text(json.dumps({
        "prev": "0" * 64, "lap": 1, "ts": "t",
        "verdict": {"passed": True, "detail": "ok", "evidence": {}}, "decision": "done",
        "budget_spent": {"laps": 1, "tokens": 7}, "attempted": True, "handoff": "",
        "gate": {"kind": "command", "source": "shipped"},
        "budget_declared": {"attempts": 3, "tokens": None, "wallclock_s": 990,
                            "wallclock_work_s": 900.0},
    }) + "\n")

    write_run_metadata(
        loop_dir=loop_dir, run_id="viamcp",
        # HALT in the outcome, `done` in the ledger row above. They deliberately DISAGREE: an audit
        # noted this test previously used DONE on both sides, so it would still pass if the headline
        # regressed to reading unhashed metadata. The assertion below now pins the source.
        outcome=Outcome(Status.HALT, "some-other-reason", 1, directory / "ledger.jsonl", "abc"),
        workspace=directory / "workspace",
    )

    for name in RECEIPT_FILES:
        assert (directory / name).is_file(), f"{name} not written by a non-CLI persist path"
    document = json.loads((directory / "receipt.json").read_text())
    assert document["run"]["status"] == "DONE", "the headline came from metadata, not the ledger"
    assert document["run"]["status_in_metadata"] == "HALT"


class TestHonestRunsAreNotAccused:
    """A round-2 fix folded `reason` into the tamper flag. `Outcome.reason` for DONE is the engine's
    canonical label ("gate-passed") while the ledger's terminal detail is the GATE's own sentence
    ("gate passed (exit 0)"). Those always differ, so EVERY honest successful run printed "Treat this
    run as suspect" — and printed it incoherently, since the banner shows the status pair, which was
    identical. All three disagreement tests used `halt` rows, where the strings happen to match, so
    the regression was invisible to the suite.
    """

    _MD_PATH = {"ledger_path": "/tmp/x/ledger.jsonl", "ledger_head": "h", "run_id": "x"}

    def _done_rows(self) -> list:
        return [{
            "lap": 1, "verdict": {"passed": True, "detail": "gate passed (exit 0)"},
            "decision": "done", "attempted": True,
            "budget_spent": {"laps": 1, "tokens": 205, "wallclock_s": 0.1},
            "budget_declared": {"attempts": 10, "tokens": None, "wallclock_s": 990},
        }]

    def test_an_honest_successful_run_is_not_flagged(self):
        document = receipt_document(
            {**self._MD_PATH, "status": "DONE", "reason": "gate-passed"}, self._done_rows(),
        )
        assert document["run"]["status_disagrees_with_metadata"] is False, (
            "an honest DONE run accused itself because the reason strings legitimately differ"
        )
        assert "Treat this run as suspect" not in receipt_markdown(document)

    def test_an_honest_paused_run_is_not_flagged(self):
        rows = self._done_rows()
        rows[0]["decision"] = "pause"
        document = receipt_document(
            {**self._MD_PATH, "status": "PAUSE", "reason": "awaiting-approval"}, rows,
        )
        assert document["run"]["status_disagrees_with_metadata"] is False
        text = receipt_markdown(document)
        assert "Treat this run as suspect" not in text
        # The ninth vacuous test, caught by audit: this asserted ONLY the absence of the banner and
        # never what the receipt actually SAID. A live PAUSE receipt read `**PAUSE** — gate passed
        # (exit 0)`: true, and positioned exactly where a reader reads "why", with the real why
        # absent. A paused run's reason is structural, not the gate's sentence.
        assert document["run"]["reason"] == "awaiting-approval"
        assert "**PAUSE** — awaiting-approval" in text
        assert "gate passed (exit 0)" not in text.split("## What this run")[0], (
            "the gate's pass sentence is standing in for the reason the run paused"
        )

    def test_a_rewritten_status_is_still_caught(self):
        """Calibration: dropping the reason comparison must not disarm the flag. Nothing is lost —
        the receipt DISPLAYS the ledger's reason, so metadata's copy is not load-bearing."""
        document = receipt_document(
            {**self._MD_PATH, "status": "DONE", "reason": "gate-passed"},
            [dict(self._done_rows()[0], decision="halt",
                  verdict={"passed": False, "detail": "max_iterations reached"})],
        )
        assert document["run"]["status_disagrees_with_metadata"] is True
        assert "Treat this run as suspect" in receipt_markdown(document)

    def test_a_real_successful_run_produces_an_unaccused_receipt(self, tmp_path, monkeypatch):
        """End to end, because the two reason strings come from different layers of the engine and
        only a real run puts both on disk."""
        import shutil

        monkeypatch.setenv("BOUNDED_LOOPS_TRUST_STORE", str(tmp_path / "trust"))
        loop_dir = tmp_path / "loop"
        shutil.copytree(Path("loops/assertion-density"), loop_dir)
        assert main(["run", str(loop_dir), "--run-id", "ok", "--yes"]) == 0

        text = (loop_dir / ".bounded-loops/runs/ok/receipt.md").read_text()
        assert "Treat this run as suspect" not in text, text[:400]


def test_a_killed_run_still_reports_what_it_spent():
    """The kill check writes its row with `budget_spent={}` before the worker runs, and a bound halt
    writes a wind-down row. Taking each segment's LAST row unconditionally reported a killed run's
    spend as unknown and dropped every token it had really spent. Under-reporting cost is the one
    direction a receipt must never fail in."""
    rows = [
        {"lap": 1, "verdict": {"passed": False, "detail": "gate failed"}, "decision": "continue",
         "attempted": True, "budget_spent": {"laps": 1, "tokens": 205, "wallclock_s": 1.5},
         "budget_declared": {"attempts": 10, "tokens": None, "wallclock_s": 990}},
        {"lap": 2, "verdict": {"passed": False, "detail": "killed"}, "decision": "killed",
         "attempted": False, "budget_spent": {},
         "budget_declared": {"attempts": 10, "tokens": None, "wallclock_s": 990}},
    ]
    document = receipt_document(
        {"run_id": "k", "status": "KILLED", "reason": "killed", "ledger_head": "h",
         "ledger_path": "/tmp/x/ledger.jsonl"}, rows,
    )
    assert document["bounds"]["tokens"]["consumed"] == 205, "a killed run's spend was lost"
    assert document["bounds"]["wallclock_s"]["consumed"] == 1.5
    assert document["run"]["status"] == "KILLED"


class TestStalenessIsNeverLeftBehind:
    """An audit escalated what had been documented as an acceptable limit: two files cannot be
    replaced atomically, so a crash between them left a NEW receipt.json beside an OLD receipt.md —
    and a paper attaches the markdown. `bl verify` reads neither file, so it stays green while the
    attachable document describes a different run. "Split-brain is a lie, not a missing file."
    """

    def _existing_pair(self, tmp_path: Path) -> None:
        (tmp_path / "receipt.json").write_text('{"run": {"status": "DONE"}}')
        (tmp_path / "receipt.md").write_text("# OLD MARKDOWN describing an earlier segment\n")

    def test_a_failure_part_way_through_removes_both_files(self, tmp_path, monkeypatch):
        import bounded_loops.application.receipt as receipt_module

        self._existing_pair(tmp_path)
        calls = {"n": 0}
        real = receipt_module._write_atomically

        def _fail_on_second(path: Path, text: str) -> None:
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("no space left on device")
            real(path, text)   # the FIRST write lands, so the pair is now mixed

        monkeypatch.setattr(receipt_module, "_write_atomically", _fail_on_second)
        with pytest.raises(OSError):
            write_receipt_artifacts(tmp_path, _metadata(tmp_path), _entries())

        assert not (tmp_path / "receipt.json").exists(), "a half-written pair survived"
        assert not (tmp_path / "receipt.md").exists(), "the OLD markdown survived beside new json"

    def test_a_failure_before_any_write_still_clears_an_earlier_receipt(self, tmp_path, capsys):
        """The resume case: metadata and the head are the new run, and the receipt on disk describes
        only the first segment. Fail-open must not preserve it."""
        self._existing_pair(tmp_path)

        def _explode() -> tuple[dict, list]:
            raise RuntimeError("ledger unreadable")

        write_receipt_artifacts_or_warn(tmp_path, _explode)

        assert not (tmp_path / "receipt.md").exists(), "a stale receipt survived a failed write"
        assert not (tmp_path / "receipt.json").exists()
        err = capsys.readouterr().err
        assert "removed rather than left stale" in err
        assert "bl receipt" in err, "the reader must be told how to re-derive it"

    def test_a_successful_write_replaces_both(self, tmp_path):
        """Calibration: the cleanup must not fire on the happy path."""
        self._existing_pair(tmp_path)
        write_receipt_artifacts(tmp_path, _metadata(tmp_path), _entries())
        assert "OLD MARKDOWN" not in (tmp_path / "receipt.md").read_text()
        assert json.loads((tmp_path / "receipt.json").read_text())["run"]["status"] == "DONE"


def test_the_receipt_module_imports_no_clock_and_no_subprocess():
    """Purity as a STRUCTURAL property, which is what the monkeypatch version could not deliver.

    An audit noted the `datetime` half of the purity test was ineffective — and it turned out worse
    than ineffective, since `datetime.datetime` is an immutable C type and patching it raises. A
    module that never imports a clock cannot read one. This check cannot go stale, needs no fixture,
    and fails the moment someone adds `import time` to stamp a receipt with a generation timestamp —
    which would make two runs of the same ledger produce different receipts.
    """
    import ast

    source = Path("bounded_loops/application/receipt.py").read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    forbidden = {"time", "datetime", "random", "subprocess", "socket", "requests", "urllib"}
    assert not (imported & forbidden), (
        f"the receipt builder imported {sorted(imported & forbidden)}; a receipt describes a run "
        "that already finished, so it must not read a clock, a network, or entropy"
    )
    # Calibrated: the check must be looking at the real module, not an empty parse.
    assert "json" in imported and "pathlib" in imported, f"parsed the wrong file? {sorted(imported)}"


class TestRegenerationDoesNotDestroyACorrectReceipt:
    """"Absence is honest" is right for a STALE artifact and wrong for a regeneration that simply
    could not run. An audit caught the round-3 fix destroying a correct, ledger-accurate pair when
    `bl receipt --write` failed on its FIRST write — nothing had been overwritten, so there was
    nothing inconsistent to clean up. Deleting a true document is a defect this code did not have
    before the fix that introduced it.
    """

    def _good_pair(self, tmp_path: Path) -> None:
        write_receipt_artifacts(tmp_path, _metadata(tmp_path), _entries())

    def test_a_failure_on_the_first_write_leaves_the_existing_pair_alone(self, tmp_path, monkeypatch):
        import bounded_loops.application.receipt as receipt_module

        self._good_pair(tmp_path)
        before = (tmp_path / "receipt.md").read_text()

        def _fail_immediately(path: Path, text: str) -> None:
            raise OSError("read-only file system")

        monkeypatch.setattr(receipt_module, "_write_atomically", _fail_immediately)
        with pytest.raises(OSError):
            write_receipt_artifacts(tmp_path, _metadata(tmp_path), _entries())

        assert (tmp_path / "receipt.md").read_text() == before, (
            "a correct receipt was destroyed by a regeneration that never wrote anything"
        )
        assert (tmp_path / "receipt.json").is_file()

    def test_the_terminal_path_still_clears_a_stale_pair(self, tmp_path, capsys):
        """The other direction must not regress: on a run's terminal path any receipt present
        describes an EARLIER state of that same run, so it is stale by construction and goes."""
        self._good_pair(tmp_path)

        def _explode() -> tuple[dict, list]:
            raise RuntimeError("ledger unreadable")

        write_receipt_artifacts_or_warn(tmp_path, _explode)

        assert not (tmp_path / "receipt.md").exists()
        assert not (tmp_path / "receipt.json").exists()
        assert "removed rather than left stale" in capsys.readouterr().err


def test_spend_is_searched_per_dimension_not_per_row():
    """A later row can carry one figure and not another. Taking the last row that had ANY figures
    reported tokens as unknown when a `{"laps": 2}` row followed a row recording 205."""
    rows = [
        {"lap": 1, "verdict": {"passed": False, "detail": "a"}, "decision": "continue",
         "attempted": True, "budget_spent": {"laps": 1, "tokens": 205, "wallclock_s": 1.5},
         "budget_declared": {"attempts": 9, "tokens": None, "wallclock_s": 990}},
        {"lap": 2, "verdict": {"passed": True, "detail": "b"}, "decision": "done",
         "attempted": True, "budget_spent": {"laps": 2},
         "budget_declared": {"attempts": 9, "tokens": None, "wallclock_s": 990}},
    ]
    bounds = receipt_document(
        {"run_id": "x", "status": "DONE", "ledger_head": "h",
         "ledger_path": "/tmp/x/ledger.jsonl"}, rows,
    )["bounds"]
    assert bounds["tokens"]["consumed"] == 205, "a partial later row hid the real token spend"
    assert bounds["wallclock_s"]["consumed"] == 1.5


def test_the_lap_table_and_the_summary_agree_about_what_was_attempted():
    """One field, one rule. The summary counted a malformed `attempted` as an attempt (`is not
    False`) while the lap table printed "no" for it (`bool(...)`), so a single document rendered the
    same field two ways. A receipt that contradicts itself is not evidence, whichever half is right.
    """
    for value in (None, "no", 0, [], {}):
        document = receipt_document(
            {"run_id": "x", "status": "DONE", "ledger_head": "h",
             "ledger_path": "/tmp/x/ledger.jsonl"},
            [{
                "lap": 1, "verdict": {"passed": True, "detail": "ok"}, "decision": "done",
                "attempted": value,
                "budget_spent": {"laps": 1, "tokens": 5, "wallclock_s": 0.1},
                "budget_declared": {"attempts": 9, "tokens": None, "wallclock_s": 990},
            }],
        )
        counted = document["bounds"]["attempts"]["consumed"]
        shown = document["laps"][0]["attempted"]
        assert counted == 1 and shown is True, (
            f"attempted={value!r}: summary counted {counted}, table showed {shown}"
        )


def test_the_integrity_note_does_not_claim_verification_reads_this_file():
    """`bl verify` never opens receipt.md or receipt.json. The note said verification showed "the
    file" was not carelessly edited — which is false, and false in the direction of reassurance."""
    integrity = receipt_document(_metadata(Path("/tmp/x")), _entries())["integrity"]
    assert "does not read this file" in integrity["note"]
    assert "that the file was not carelessly edited" not in integrity["note"]


def test_a_cleanup_that_could_not_delete_says_so_instead_of_claiming_success(tmp_path, capsys):
    """The cleanup was best-effort and then printed "removed rather than left stale" regardless — an
    audit called that lying about success. If a stale receipt survives, the reader must be told, in
    the one case where the surviving file is a confident document about a run that has moved on.
    """
    import bounded_loops.application.receipt as receipt_module

    (tmp_path / "receipt.md").write_text("# OLD MARKDOWN\n")
    (tmp_path / "receipt.json").write_text("{}")

    def _refuse(self: Path, *args: object, **kwargs: object) -> None:
        raise OSError("operation not permitted")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(Path, "unlink", _refuse)
    try:
        receipt_module.write_receipt_artifacts_or_warn(
            tmp_path, lambda: (_ for _ in ()).throw(RuntimeError("ledger unreadable")),
        )
    finally:
        monkeypatch.undo()

    err = capsys.readouterr().err
    assert "could not remove" in err, f"the cleanup claimed a success it did not achieve: {err}"
    assert "receipt.md" in err and "must not be trusted" in err
    assert "removed rather than left stale" not in err
    assert (tmp_path / "receipt.md").exists(), "fixture invalid: the file should have survived"
