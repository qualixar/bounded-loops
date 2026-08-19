"""
Acceptance tests for the bl CLI.

Tests invoke main() with explicit argv to avoid subprocess overhead.
All filesystem I/O is directed to tmp_path fixtures.
"""
import json
import pytest
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

from bounded_loops.cli import _print_outcome, main
from bounded_loops.application.loop_audit import LoopAuditResult
from bounded_loops.domain.models import Status, Outcome
from bounded_loops.domain.errors import ManifestError


def test_preflight_json_reports_observation_without_claiming_admission(capsys):
    code = main(["preflight", "--profile", "m4-external-review", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["schema_version"] == 1
    assert payload["runners"][0]["admission"] == "denied"
    assert payload["runners"][0]["available"] is False


# ── top-level CLI contract ───────────────────────────────────────────────────

def test_version_flag_reports_release_version(capsys):
    """Asserted against the package's own version rather than a literal.

    A hardcoded string here means every release breaks a test that is not describing a defect,
    and the fix is to edit the expectation — which trains people to edit expectations. The
    release contract test is what pins the version to pyproject; this one only has to prove
    `--version` reports it.
    """
    from bounded_loops import __version__

    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])

    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"bl {__version__}"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_outcome(status: Status, laps: int = 2, ledger: Path = Path("/tmp/.ledger.jsonl")):
    return Outcome(
        status=status,
        reason="gate-passed" if status == Status.DONE else "halt:max-iterations",
        laps=laps,
        ledger_path=ledger,
    )


def _patch_run(tmp_path, outcome: Outcome):
    """Context manager: patches manifest.load + composition.wire + use_case.run.

    Also patches _confirm_trust to auto-approve: the trust
    gate is tested explicitly and in isolation in TestTrustConfirmation below;
    every OTHER test in this file is about exit codes / flag passthrough /
    output shape and should not have to also pass --yes or fake a TTY just
    to get past the confirmation prompt.
    """
    from contextlib import contextmanager
    @contextmanager
    def _ctx():
        fake_manifest = MagicMock()
        fake_use_case = MagicMock()
        fake_use_case.run.return_value = outcome

        with patch("bounded_loops.cli.manifest_load", return_value=fake_manifest), \
             patch("bounded_loops.cli.wire", return_value=fake_use_case), \
             patch("bounded_loops.cli._confirm_trust", return_value=True):
            yield fake_manifest, fake_use_case
    return _ctx()


# ── bl run exit codes ──────────────────────────────────────────────────────────

class TestBLRunExitCodes:
    def test_done_output_explains_verified_result_and_next_step(self, capsys):
        _print_outcome(_make_outcome(Status.DONE, laps=3), as_json=False)

        output = capsys.readouterr().out
        assert "Gate verified:" in output
        assert "3 laps" in output
        assert "Next:" in output

    def test_run_done_exits_0(self, tmp_path):
        """bl run → DONE → exit 0."""
        outcome = _make_outcome(Status.DONE)
        (tmp_path / "loop.yaml").write_text("name: t\n")
        with _patch_run(tmp_path, outcome):
            code = main(["run", str(tmp_path)])
        assert code == 0

    def test_run_halt_exits_1(self, tmp_path):
        """bl run → HALT → exit 1."""
        outcome = _make_outcome(Status.HALT)
        (tmp_path / "loop.yaml").write_text("name: t\n")
        with _patch_run(tmp_path, outcome):
            code = main(["run", str(tmp_path)])
        assert code == 1

    def test_run_pause_exits_1(self, tmp_path):
        """bl run → PAUSE → exit 1."""
        outcome = _make_outcome(Status.PAUSE)
        (tmp_path / "loop.yaml").write_text("name: t\n")
        with _patch_run(tmp_path, outcome):
            code = main(["run", str(tmp_path)])
        assert code == 1

    def test_run_killed_exits_1(self, tmp_path):
        """bl run → KILLED → exit 1."""
        outcome = _make_outcome(Status.KILLED)
        (tmp_path / "loop.yaml").write_text("name: t\n")
        with _patch_run(tmp_path, outcome):
            code = main(["run", str(tmp_path)])
        assert code == 1

    def test_run_missing_dir_exits_2(self):
        """bl run on nonexistent dir → exit 2 (ManifestError class)."""
        code = main(["run", "/nonexistent/loop/dir"])
        assert code == 2

    def test_run_manifest_error_exits_2(self, tmp_path):
        """bl run → ManifestError from manifest.load → exit 2."""
        (tmp_path / "loop.yaml").write_text("name: t\n")
        with patch("bounded_loops.cli.manifest_load",
                   side_effect=ManifestError("bad manifest")):
            code = main(["run", str(tmp_path)])
        assert code == 2

    def test_run_manifest_error_from_wire_exits_2(self, tmp_path):
        """bl run → ManifestError from wire() (unknown gate.kind) → exit 2.
        Patches _confirm_trust=True so this test actually
        reaches wire() rather than accidentally exiting 2 via the trust gate
        for the wrong reason."""
        (tmp_path / "loop.yaml").write_text("name: t\n")
        fake_manifest = MagicMock()
        with patch("bounded_loops.cli.manifest_load", return_value=fake_manifest), \
             patch("bounded_loops.cli._confirm_trust", return_value=True), \
             patch("bounded_loops.cli.wire",
                   side_effect=ManifestError("Unknown gate.kind 'bad'")):
            code = main(["run", str(tmp_path)])
        assert code == 2

    def test_run_unexpected_exception_exits_3(self, tmp_path):
        """bl run → unhandled RuntimeError in use_case.run() → exit 3.
        Patches _confirm_trust=True — without it this
        test would falsely pass for the wrong reason (trust-gate exit 2
        masquerading as a route to exit 3 that's never actually exercised)."""
        (tmp_path / "loop.yaml").write_text("name: t\n")
        fake_manifest = MagicMock()
        fake_use_case = MagicMock()
        fake_use_case.run.side_effect = RuntimeError("agent subprocess crashed")
        with patch("bounded_loops.cli.manifest_load", return_value=fake_manifest), \
             patch("bounded_loops.cli._confirm_trust", return_value=True), \
             patch("bounded_loops.cli.wire", return_value=fake_use_case):
            code = main(["run", str(tmp_path)])
        assert code == 3


# ── bl run --json output shape ─────────────────────────────────────────────────

class TestBLRunJsonShape:
    def test_run_json_output_keys(self, tmp_path, capsys):
        """bl run --json → stdout is valid JSON with required keys."""
        outcome = _make_outcome(Status.DONE, laps=3,
                                ledger=tmp_path / ".ledger.jsonl")
        (tmp_path / "loop.yaml").write_text("name: t\n")
        with _patch_run(tmp_path, outcome):
            code = main(["run", str(tmp_path), "--json"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["subcommand"] == "run"
        assert data["status"] == "DONE"
        assert data["reason"] == "gate-passed"
        assert data["laps"] == 3
        assert "ledger_path" in data
        assert code == 0

    def test_run_json_halt_status(self, tmp_path, capsys):
        """bl run --json HALT → status field is 'HALT', exit 1."""
        outcome = _make_outcome(Status.HALT, laps=5)
        (tmp_path / "loop.yaml").write_text("name: t\n")
        with _patch_run(tmp_path, outcome):
            code = main(["run", str(tmp_path), "--json"])
        data = json.loads(capsys.readouterr().out)
        assert data["status"] == "HALT"
        assert code == 1


# ── bl run flag passthrough ────────────────────────────────────────────────────

class TestBLRunFlagPassthrough:
    def test_runner_flag_passed_to_wire(self, tmp_path):
        """--runner codex is passed as runner_override to wire().
        Patches _confirm_trust=True — no --gate-override
        here, so without this the trust gate would return early (exit 2,
        stdin not a tty in test env) and wire() would never be called."""
        outcome = _make_outcome(Status.DONE)
        (tmp_path / "loop.yaml").write_text("name: t\n")
        fake_manifest = MagicMock()
        fake_use_case = MagicMock()
        fake_use_case.run.return_value = outcome
        with patch("bounded_loops.cli.manifest_load", return_value=fake_manifest) as _ml, \
             patch("bounded_loops.cli._confirm_trust", return_value=True), \
             patch("bounded_loops.cli.wire", return_value=fake_use_case) as mock_wire:
            main(["run", str(tmp_path), "--runner", "codex"])
        mock_wire.assert_called_once_with(
            fake_manifest,
            runner_override="codex",
            gate_cmd_override=None,
            max_iterations_override=None,
            keep_workspace=False,
            run_id=None,
            resume=False,
            redaction=ANY,
        )

    def test_runner_flag_accepts_python_callable(self, tmp_path):
        """--runner python_callable is a valid argparse choice
        and is passed through to wire() unchanged."""
        outcome = _make_outcome(Status.DONE)
        (tmp_path / "loop.yaml").write_text("name: t\n")
        fake_manifest = MagicMock()
        fake_use_case = MagicMock()
        fake_use_case.run.return_value = outcome
        with patch("bounded_loops.cli.manifest_load", return_value=fake_manifest), \
             patch("bounded_loops.cli._confirm_trust", return_value=True), \
             patch("bounded_loops.cli.wire", return_value=fake_use_case) as mock_wire:
            main(["run", str(tmp_path), "--runner", "python_callable"])
        mock_wire.assert_called_once_with(
            fake_manifest,
            runner_override="python_callable",
            gate_cmd_override=None,
            max_iterations_override=None,
            keep_workspace=False,
            run_id=None,
            resume=False,
            redaction=ANY,
        )

    def test_runner_flag_accepts_antigravity(self, tmp_path):
        """--runner antigravity is a valid argparse choice
        and is passed through to wire() unchanged."""
        outcome = _make_outcome(Status.DONE)
        (tmp_path / "loop.yaml").write_text("name: t\n")
        fake_manifest = MagicMock()
        fake_use_case = MagicMock()
        fake_use_case.run.return_value = outcome
        with patch("bounded_loops.cli.manifest_load", return_value=fake_manifest), \
             patch("bounded_loops.cli._confirm_trust", return_value=True), \
             patch("bounded_loops.cli.wire", return_value=fake_use_case) as mock_wire:
            main(["run", str(tmp_path), "--runner", "antigravity"])
        mock_wire.assert_called_once_with(
            fake_manifest,
            runner_override="antigravity",
            gate_cmd_override=None,
            max_iterations_override=None,
            keep_workspace=False,
            run_id=None,
            resume=False,
            redaction=ANY,
        )

    def test_cli_runner_choices_include_all_new_kinds(self, capsys):
        """Every runner kind, old and new, must
        appear in `bl run --help`'s --runner choices list."""
        with pytest.raises(SystemExit):
            main(["run", "--help"])
        captured = capsys.readouterr()
        for kind in ["stub", "shell", "claude-code", "codex", "python_callable", "antigravity", "docker", "worktree"]:
            assert kind in captured.out

    def test_gate_override_passed_to_wire(self, tmp_path):
        """--gate-override 'npm test' passed as gate_cmd_override."""
        outcome = _make_outcome(Status.DONE)
        (tmp_path / "loop.yaml").write_text("name: t\n")
        fake_manifest = MagicMock()
        fake_use_case = MagicMock()
        fake_use_case.run.return_value = outcome
        with patch("bounded_loops.cli.manifest_load", return_value=fake_manifest), \
             patch("bounded_loops.cli.wire", return_value=fake_use_case) as mock_wire:
            main(["run", str(tmp_path), "--gate-override", "npm test"])
        mock_wire.assert_called_once_with(
            fake_manifest,
            runner_override=None,
            gate_cmd_override="npm test",
            max_iterations_override=None,
            keep_workspace=False,
            run_id=None,
            resume=False,
            redaction=ANY,
        )

    def test_max_iterations_passed_to_wire(self, tmp_path):
        """--max-iterations 1 passed as max_iterations_override=1.
        Patches _confirm_trust=True for the same reason as the test above."""
        outcome = _make_outcome(Status.DONE)
        (tmp_path / "loop.yaml").write_text("name: t\n")
        fake_manifest = MagicMock()
        fake_use_case = MagicMock()
        fake_use_case.run.return_value = outcome
        with patch("bounded_loops.cli.manifest_load", return_value=fake_manifest), \
             patch("bounded_loops.cli._confirm_trust", return_value=True), \
             patch("bounded_loops.cli.wire", return_value=fake_use_case) as mock_wire:
            main(["run", str(tmp_path), "--max-iterations", "1"])
        mock_wire.assert_called_once_with(
            fake_manifest,
            runner_override=None,
            gate_cmd_override=None,
            max_iterations_override=1,
            keep_workspace=False,
            run_id=None,
            resume=False,
            redaction=ANY,
        )

    def test_run_id_and_resume_passed_to_wire(self, tmp_path):
        outcome = _make_outcome(Status.DONE)
        (tmp_path / "loop.yaml").write_text("name: t\n")
        fake_manifest = MagicMock()
        fake_manifest.loop_dir = tmp_path
        fake_use_case = MagicMock()
        fake_use_case.run.return_value = outcome
        fake_use_case._workspace = tmp_path / "workspace"
        fake_use_case._deps.ledger.path.return_value = outcome.ledger_path
        with patch("bounded_loops.cli.manifest_load", return_value=fake_manifest), \
             patch("bounded_loops.cli._confirm_trust", return_value=True), \
             patch("bounded_loops.cli.begin_run") as mock_begin, \
             patch("bounded_loops.cli.write_run_metadata") as mock_metadata, \
             patch("bounded_loops.cli.wire", return_value=fake_use_case) as mock_wire:
            code = main(["run", str(tmp_path), "--run-id", "r1", "--resume"])
        assert code == 0
        mock_wire.assert_called_once_with(
            fake_manifest,
            runner_override=None,
            gate_cmd_override=None,
            max_iterations_override=None,
            keep_workspace=False,
            run_id="r1",
            resume=True,
            redaction=ANY,
        )
        mock_begin.assert_called_once_with(
            loop_dir=tmp_path,
            run_id="r1",
            workspace=fake_use_case._workspace,
            ledger_path=outcome.ledger_path,
        )
        mock_metadata.assert_called_once()


# ── bl lint exit codes ─────────────────────────────────────────────────────────

class TestBLLint:
    def test_lint_all_pass_exits_0(self, tmp_path):
        """All manifests valid → exit 0."""
        loop_a = tmp_path / "loop-a"
        loop_a.mkdir()
        (loop_a / "loop.yaml").write_text("name: a\n")
        with patch("bounded_loops.cli.manifest_load", return_value=MagicMock()):
            code = main(["lint", str(loop_a)])
        assert code == 0

    def test_lint_one_fail_exits_1(self, tmp_path):
        """One manifest fails validation → exit 1."""
        loop_a = tmp_path / "loop-a"
        loop_a.mkdir()
        (loop_a / "loop.yaml").write_text("name: a\n")
        with patch("bounded_loops.cli.manifest_load",
                   side_effect=ManifestError("runner.default must be stub or shell")):
            code = main(["lint", str(loop_a)])
        assert code == 1


class TestBLAuditLoops:
    def test_audit_loops_json_output(self, tmp_path, capsys):
        loop_dir = tmp_path / "loop-a"
        loop_dir.mkdir()
        with patch("bounded_loops.cli.audit_loops") as mock_audit:
            mock_audit.return_value = [LoopAuditResult(
                path=str(loop_dir), name="loop-a", passed=True,
            )]
            code = main(["audit-loops", str(tmp_path), "--json"])
        assert code == 0
        assert "loop-a" in capsys.readouterr().out

    def test_audit_loops_accepts_multiple_dirs(self, tmp_path):
        first = tmp_path / "a"
        second = tmp_path / "b"
        first.mkdir()
        second.mkdir()
        with patch("bounded_loops.cli.audit_loops") as mock_audit:
            mock_audit.return_value = []
            code = main(["audit-loops", str(first), str(second)])
        assert code == 0
        assert mock_audit.call_count == 2


class TestBLShowAndGates:
    def test_doctor_json_reports_core_and_optional_capabilities(self, capsys):
        code = main(["doctor", "--json"])

        assert code == 0
        data = json.loads(capsys.readouterr().out)
        assert data["python"]["available"] is True
        assert data["pytest"]["available"] is True
        assert {item["name"] for item in data["runners"]} >= {"stub", "codex"}
        assert data["gates"]

    def test_show_json_output(self, tmp_path, capsys):
        with patch("bounded_loops.cli.show_loop", return_value={
            "name": "loop-a",
            "path": str(tmp_path),
            "role": ["testing"],
            "pattern": "evaluator-optimizer",
            "rung": "L1",
            "runner": {"kind": "stub"},
            "gate": {"kind": "pytest"},
            "approval_required": False,
            "production_bounds": None,
            "risk": [],
            "content_hash": "abc",
        }):
            code = main(["show", str(tmp_path), "--json"])
        assert code == 0
        assert json.loads(capsys.readouterr().out)["name"] == "loop-a"

    def test_gates_json_output(self, capsys):
        with patch("bounded_loops.cli.list_gates", return_value=[{
            "kind": "command", "available": True, "description": "x", "dependencies": [],
        }]):
            code = main(["gates", "--json"])
        assert code == 0
        assert json.loads(capsys.readouterr().out)["gates"][0]["kind"] == "command"

    def test_runs_json_output(self, tmp_path, capsys):
        with patch("bounded_loops.cli.list_runs", return_value=[{"run_id": "r1", "status": "DONE"}]):
            code = main(["runs", str(tmp_path), "--json"])
        assert code == 0
        assert json.loads(capsys.readouterr().out)["runs"][0]["run_id"] == "r1"

    def test_runs_show_pretty_prints_per_lap_receipt(self, tmp_path, capsys):
        run_dir = tmp_path / ".bounded-loops" / "runs" / "r1"
        run_dir.mkdir(parents=True)
        (run_dir / "metadata.json").write_text(
            json.dumps({
                "run_id": "r1",
                "status": "DONE",
                "reason": "gate-passed",
                "laps": 2,
                "workspace": str(run_dir / "workspace"),
                "ledger_path": str(run_dir / "ledger.jsonl"),
            }),
            encoding="utf-8",
        )
        entries = [
            {
                "lap": 1,
                "verdict": {"passed": False, "detail": "1 failed"},
                "decision": "continue",
                "budget_spent": {"laps": 1, "tokens": 20, "wallclock_s": 1.5},
            },
            {
                "lap": 2,
                "verdict": {"passed": True, "detail": "2 passed"},
                "decision": "done",
                "budget_spent": {"laps": 2, "tokens": 35, "wallclock_s": 2.75},
            },
        ]
        (run_dir / "ledger.jsonl").write_text(
            "".join(json.dumps(entry) + "\n" for entry in entries),
            encoding="utf-8",
        )

        code = main(["runs", str(tmp_path), "--show", "r1"])

        output = capsys.readouterr().out
        assert code == 0
        assert "Run r1: DONE" in output
        assert "Lap 1" in output and "FAIL" in output and "continue" in output
        assert "Lap 2" in output and "PASS" in output and "done" in output
        assert "tokens=35" in output and "wallclock=2.75s" in output

    def test_runs_show_names_the_gate_that_decided_each_lap(self, tmp_path, capsys):
        """Drives the CLI, deliberately — the point of gate provenance is that a HUMAN reading a
        receipt can see what decided it. A test that read the ledger file instead would pass while
        the value reached no user-visible path, which is exactly how the wave-2 gate identity sat
        unused: a unit test cannot catch an orphaned capability, because the test IS the missing
        caller."""
        run_dir = tmp_path / ".bounded-loops" / "runs" / "r1"
        run_dir.mkdir(parents=True)
        (run_dir / "metadata.json").write_text(json.dumps({
            "run_id": "r1", "status": "DONE", "reason": "gate-passed", "laps": 1,
            "workspace": str(run_dir / "workspace"),
            "ledger_path": str(run_dir / "ledger.jsonl"),
        }), encoding="utf-8")
        (run_dir / "ledger.jsonl").write_text(json.dumps({
            "lap": 1, "verdict": {"passed": True, "detail": "ok"}, "decision": "done",
            "budget_spent": {"laps": 1, "tokens": 5, "wallclock_s": 0.1},
            "gate": {"kind": "acme-check", "source": "plugin",
                     "distribution": "acme-gates", "implementation": "AcmeGate"},
        }) + "\n", encoding="utf-8")

        code = main(["runs", str(tmp_path), "--show", "r1"])

        output = capsys.readouterr().out
        assert code == 0
        assert "acme-check" in output, "the receipt does not say WHICH gate decided the lap"
        assert "plugin" in output, "the receipt does not say the gate was third-party"
        assert "acme-gates" in output, "the receipt does not name the distribution"

    def test_runs_show_still_renders_a_receipt_written_before_provenance_existed(
        self, tmp_path, capsys,
    ):
        """Calibration: the new line must be ABSENT, not the string "None", for older runs."""
        run_dir = tmp_path / ".bounded-loops" / "runs" / "r1"
        run_dir.mkdir(parents=True)
        (run_dir / "metadata.json").write_text(json.dumps({
            "run_id": "r1", "status": "DONE", "reason": "gate-passed", "laps": 1,
            "workspace": str(run_dir / "workspace"),
            "ledger_path": str(run_dir / "ledger.jsonl"),
        }), encoding="utf-8")
        (run_dir / "ledger.jsonl").write_text(json.dumps({
            "lap": 1, "verdict": {"passed": True, "detail": "ok"}, "decision": "done",
            "budget_spent": {"laps": 1, "tokens": 5, "wallclock_s": 0.1},
        }) + "\n", encoding="utf-8")

        code = main(["runs", str(tmp_path), "--show", "r1"])

        output = capsys.readouterr().out
        assert code == 0
        assert "Lap 1" in output and "PASS" in output
        assert "gate:" not in output and "None" not in output

    def _write_run(self, tmp_path, rows, status="DONE", reason="gate-passed"):
        run_dir = tmp_path / ".bounded-loops" / "runs" / "r1"
        run_dir.mkdir(parents=True)
        (run_dir / "metadata.json").write_text(json.dumps({
            "run_id": "r1", "status": status, "reason": reason, "laps": len(rows),
            "workspace": str(run_dir / "workspace"),
            "ledger_path": str(run_dir / "ledger.jsonl"),
        }), encoding="utf-8")
        (run_dir / "ledger.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    def test_runs_show_reads_consumption_against_the_declared_ceiling(self, tmp_path, capsys):
        """The product is called bounded-loops. Until this line existed its own receipt showed
        `tokens=205` with nothing to read it against, and a spend figure alone cannot support the
        claim the name makes."""
        self._write_run(tmp_path, [{
            "lap": 1, "verdict": {"passed": True, "detail": "ok"}, "decision": "done",
            "attempted": True,
            "budget_spent": {"laps": 1, "tokens": 205, "wallclock_s": 0.5},
            "budget_declared": {"attempts": 10, "tokens": None, "wallclock_s": 990},
        }])

        assert main(["runs", str(tmp_path), "--show", "r1"]) == 0
        output = capsys.readouterr().out
        assert "attempts 1/10" in output
        assert "wallclock 0.5s/990s" in output
        assert "tokens 205/no ceiling" in output, (
            "an undeclared ceiling must say so; printing nothing reads as if a ceiling applied"
        )

    def test_a_bound_that_held_exactly_is_not_reported_as_overrun(self, tmp_path, capsys):
        """THE case this computation exists for. A ceiling halt writes a final row on which no work
        was attempted, so counting ROWS reports 3 attempts against a declared 2 — a bound that held
        exactly, shown as 1.5x over. `attempted` makes it computable; the receipt must do it."""
        self._write_run(tmp_path, [
            {"lap": 1, "verdict": {"passed": False, "detail": "x"}, "decision": "continue",
             "attempted": True, "budget_spent": {"laps": 1, "tokens": 10, "wallclock_s": 0.1},
             "budget_declared": {"attempts": 2, "tokens": None, "wallclock_s": 990}},
            {"lap": 2, "verdict": {"passed": False, "detail": "x"}, "decision": "continue",
             "attempted": True, "budget_spent": {"laps": 2, "tokens": 20, "wallclock_s": 0.2},
             "budget_declared": {"attempts": 2, "tokens": None, "wallclock_s": 990}},
            {"lap": 3, "verdict": {"passed": False, "detail": "halt"}, "decision": "halt",
             "attempted": False, "budget_spent": {"laps": 3, "tokens": 20, "wallclock_s": 0.3},
             "budget_declared": {"attempts": 2, "tokens": None, "wallclock_s": 990}},
        ], status="HALT", reason="max_iterations 2 reached at lap 3")

        assert main(["runs", str(tmp_path), "--show", "r1"]) == 0
        output = capsys.readouterr().out
        assert "attempts 2/2" in output, f"a bound that held exactly was misreported: {output}"
        assert "attempts 3/2" not in output

    def test_runs_show_omits_the_bounds_line_for_a_run_recorded_before_it_existed(
        self, tmp_path, capsys,
    ):
        """Calibration: absent, not "None/None"."""
        self._write_run(tmp_path, [{
            "lap": 1, "verdict": {"passed": True, "detail": "ok"}, "decision": "done",
            "budget_spent": {"laps": 1, "tokens": 5, "wallclock_s": 0.1},
        }])

        assert main(["runs", str(tmp_path), "--show", "r1"]) == 0
        output = capsys.readouterr().out
        assert "Lap 1" in output and "PASS" in output
        assert "bounds:" not in output and "None" not in output

    def test_lint_contrib_accepts_reference_loop(self):
        repo_root = Path(__file__).resolve().parents[1]
        code = main([
            "lint",
            str(repo_root / "loops" / "convergence-demo"),
            "--contrib",
        ])
        assert code == 0

    def test_lint_nonexistent_dir_exits_1(self, tmp_path):
        """Nonexistent loop-dir → recorded as failure → exit 1."""
        code = main(["lint", str(tmp_path / "does-not-exist")])
        assert code == 1

    def test_lint_rejects_product_gate_default(self, tmp_path):
        """
        Lint must reject a manifest where gate.kind is a Qualixar product.
        This test verifies the lint pipeline catches the ManifestError that
        manifest.load() raises for product-default manifests.
        """
        loop_a = tmp_path / "loop-a"
        loop_a.mkdir()
        (loop_a / "loop.yaml").write_text("name: a\n")
        with patch(
            "bounded_loops.cli.manifest_load",
            side_effect=ManifestError(
                "gate.kind 'agentassert' is a Qualixar product and cannot "
                "be used as the manifest default"
            ),
        ):
            code = main(["lint", str(loop_a)])
        assert code == 1

    def test_lint_rejects_non_keyless_runner_default(self, tmp_path):
        """
        Lint must reject runner.default=claude-code (not keyless).
        runner.default MUST be stub or shell, and bl lint must enforce
        this rule.
        """
        loop_a = tmp_path / "loop-a"
        loop_a.mkdir()
        (loop_a / "loop.yaml").write_text("name: a\n")
        with patch(
            "bounded_loops.cli.manifest_load",
            side_effect=ManifestError(
                "runner.default must be 'stub' or 'shell', got 'claude-code'"
            ),
        ):
            code = main(["lint", str(loop_a)])
        assert code == 1

    def test_lint_multiple_dirs_partial_fail(self, tmp_path):
        """3 dirs: 2 pass, 1 fail → exit 1."""
        dirs = []
        for name in ("a", "b", "c"):
            d = tmp_path / name
            d.mkdir()
            (d / "loop.yaml").write_text(f"name: {name}\n")
            dirs.append(d)

        call_count = [0]
        def side_effect(p):
            call_count[0] += 1
            if call_count[0] == 2:
                raise ManifestError("bad manifest")
            return MagicMock()

        with patch("bounded_loops.cli.manifest_load", side_effect=side_effect):
            code = main(["lint"] + [str(d) for d in dirs])
        assert code == 1

    def test_lint_has_no_json_flag(self, tmp_path):
        """--json is bl-run-only. bl lint --json is a usage error."""
        loop_a = tmp_path / "loop-a"
        loop_a.mkdir()
        (loop_a / "loop.yaml").write_text("name: a\n")
        with pytest.raises(SystemExit):
            main(["lint", str(loop_a), "--json"])


# ── bl list ────────────────────────────────────────────────────────────────────

class TestBLList:
    def test_list_exits_0_when_no_repo_root_found(self, tmp_path, capsys, monkeypatch):
        """bl list run outside any bounded-loops repo gives installed-user guidance."""
        monkeypatch.chdir(tmp_path)
        code = main(["list"])
        assert code == 0
        out = capsys.readouterr().out
        assert "No loops found." in out
        assert "bl new --list" in out
        assert "git clone https://github.com/qualixar/bounded-loops" in out

    def test_list_finds_loop_under_loops_subfolder(self, tmp_path, capsys, monkeypatch):
        """Discovery walks UP for pyproject.toml then
        globs loops/*/loop.yaml — never an unbounded cwd.rglob."""
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        loop_dir = tmp_path / "loops" / "my-loop"
        loop_dir.mkdir(parents=True)
        (loop_dir / "loop.yaml").write_text("name: my-loop\n")
        # cwd is a subdirectory of the repo root — exactly the "first thing a
        # user tries right after cd-ing into their new loop" scenario.
        monkeypatch.chdir(loop_dir)
        fake_manifest = MagicMock()
        fake_manifest.name = "my-loop"
        fake_manifest.raw = {"role": ["backend"]}
        fake_manifest.rung.value = "L2"
        fake_manifest.gate_kind = "pytest"
        with patch("bounded_loops.cli.manifest_load", return_value=fake_manifest):
            main(["list"])
        out = capsys.readouterr().out
        assert "my-loop" in out
        assert "pytest" in out

    def test_list_does_not_descend_into_arbitrary_subdirs_outside_loops(self, tmp_path, monkeypatch):
        """A loop.yaml planted OUTSIDE loops/ (e.g. inside a vendored
        dependency tree) must NOT be discovered — this is the exact
        unbounded-scan risk this bounded discovery exists to prevent."""
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        planted = tmp_path / "node_modules" / "some-pkg"
        planted.mkdir(parents=True)
        (planted / "loop.yaml").write_text("name: planted\n")
        monkeypatch.chdir(tmp_path)
        with patch("bounded_loops.cli.manifest_load") as mock_load:
            main(["list"])
        mock_load.assert_not_called()

    def test_list_has_no_json_flag(self, tmp_path, monkeypatch):
        """--json is bl-run-only. bl list --json is a usage error."""
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit):
            main(["list", "--json"])


# ── bl run trust confirmation (security fix) ──────────────────────

class TestTrustConfirmation:
    def test_preview_prints_effective_runner_override(self, tmp_path, capsys):
        outcome = _make_outcome(Status.DONE)
        (tmp_path / "loop.yaml").write_text("name: t\n")
        fake_manifest = MagicMock()
        fake_manifest.name = "t"
        fake_manifest.runner_kind = "stub"
        fake_manifest.gate_kind = "command"
        fake_manifest.gate_config = {"run": "true"}
        fake_use_case = MagicMock()
        fake_use_case.run.return_value = outcome
        with patch("bounded_loops.cli.manifest_load", return_value=fake_manifest), \
             patch("bounded_loops.cli.wire", return_value=fake_use_case):
            code = main(["run", str(tmp_path), "--runner", "codex", "--yes"])

        assert code == 0
        assert "runner : codex" in capsys.readouterr().out

    def test_yes_flag_skips_prompt(self, tmp_path):
        outcome = _make_outcome(Status.DONE)
        (tmp_path / "loop.yaml").write_text("name: t\n")
        with _patch_run(tmp_path, outcome):
            code = main(["run", str(tmp_path), "--yes"])
        assert code == 0

    def test_gate_override_skips_prompt(self, tmp_path):
        """The user typed the command themselves via --gate-override — no
        need to re-confirm a command they just supplied on the command line."""
        outcome = _make_outcome(Status.DONE)
        (tmp_path / "loop.yaml").write_text("name: t\n")
        with _patch_run(tmp_path, outcome):
            code = main(["run", str(tmp_path), "--gate-override", "true"])
        assert code == 0

    def test_non_interactive_without_yes_fails_closed(self, tmp_path, monkeypatch):
        """No --yes, and stdin is not a TTY (the CI case) → exit 2, never
        silently proceeds. Fail closed, not open."""
        fake_manifest = MagicMock()
        fake_manifest.gate_config = {"run": "pytest -q"}
        fake_manifest.name = "t"
        fake_manifest.runner_kind = "stub"
        (tmp_path / "loop.yaml").write_text("name: t\n")
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        with patch("bounded_loops.cli.manifest_load", return_value=fake_manifest):
            code = main(["run", str(tmp_path)])
        assert code == 2

    def test_genuine_interactive_yes_records_trust(self, tmp_path, monkeypatch):
        """A REAL interactive 'y' answer (tty +
        typed 'y', not --yes) must call record_trust exactly once with the
        manifest's loop_dir and gate command."""
        outcome = _make_outcome(Status.DONE)
        (tmp_path / "loop.yaml").write_text("name: t\n")
        fake_manifest = MagicMock()
        fake_manifest.name = "t"
        fake_manifest.runner_kind = "stub"
        fake_manifest.gate_kind = "command"
        fake_manifest.gate_config = {"run": "true"}
        fake_manifest.loop_dir = tmp_path
        fake_use_case = MagicMock()
        fake_use_case.run.return_value = outcome
        with patch("bounded_loops.cli.manifest_load", return_value=fake_manifest), \
             patch("bounded_loops.cli.wire", return_value=fake_use_case), \
             patch("bounded_loops.cli_preconditions.record_trust") as mock_record_trust, \
             patch("sys.stdin.isatty", return_value=True), \
             patch("builtins.input", return_value="y"):
            code = main(["run", str(tmp_path)])
        assert code == 0
        mock_record_trust.assert_called_once_with(tmp_path, "true")

    def test_interactive_no_answer_does_not_record_trust(self, tmp_path):
        """A genuine interactive session where the human answers 'n' must
        NOT record trust — only an affirmative answer is a review event."""
        (tmp_path / "loop.yaml").write_text("name: t\n")
        fake_manifest = MagicMock()
        fake_manifest.name = "t"
        fake_manifest.runner_kind = "stub"
        fake_manifest.gate_kind = "command"
        fake_manifest.gate_config = {"run": "true"}
        fake_manifest.loop_dir = tmp_path
        with patch("bounded_loops.cli.manifest_load", return_value=fake_manifest), \
             patch("bounded_loops.cli_preconditions.record_trust") as mock_record_trust, \
             patch("sys.stdin.isatty", return_value=True), \
             patch("builtins.input", return_value="n"):
            code = main(["run", str(tmp_path)])
        assert code == 2
        mock_record_trust.assert_not_called()


# ── No subcommand ──────────────────────────────────────────────────────────────

class TestNoSubcommand:
    def test_no_subcommand_exits_1(self):
        """bl (no subcommand) → exit 1 (prints help)."""
        code = main([])
        assert code == 1


# ── Worked examples (documentation tests) ─────────────────────────────────────

class TestWorkedExamples:
    def test_worked_example_bl_run_done(self, tmp_path, capsys):
        """
        Worked example: bl run loops/bug-fix-red-green → DONE

        Expected stdout (human): '✓ [DONE] gate-passed (laps: 2)  ledger: ...'
        Expected exit: 0
        """
        outcome = Outcome(
            status=Status.DONE,
            reason="gate-passed",
            laps=2,
            ledger_path=tmp_path / ".ledger.jsonl",
        )
        (tmp_path / "loop.yaml").write_text("name: bug-fix-red-green\n")
        with _patch_run(tmp_path, outcome):
            code = main(["run", str(tmp_path)])
        out = capsys.readouterr().out
        assert "DONE" in out
        assert "gate-passed" in out
        assert code == 0

    def test_worked_example_bl_run_halt(self, tmp_path, capsys):
        """
        Worked example: bl run loops/bug-fix-red-green → HALT (max iterations)

        Expected stdout: '✗ [HALT] halt:max-iterations (laps: 5)  ledger: ...'
        Expected exit: 1
        """
        outcome = Outcome(
            status=Status.HALT,
            reason="halt:max-iterations",
            laps=5,
            ledger_path=tmp_path / ".ledger.jsonl",
        )
        (tmp_path / "loop.yaml").write_text("name: bug-fix-red-green\n")
        with _patch_run(tmp_path, outcome):
            code = main(["run", str(tmp_path)])
        out = capsys.readouterr().out
        assert "HALT" in out
        assert code == 1


class TestReceiptEndToEnd:
    """A real run, then the receipt a human reads. The synthetic receipt tests above pin the
    ARITHMETIC; these pin that the harness actually produces the inputs that arithmetic needs —
    that `composition` pushes the resolved ceilings in, and that the controller marks the
    wind-down row `attempted: false`. Neither is visible to a test that writes its own ledger.
    """

    def _loop(self, tmp_path):
        import shutil
        from pathlib import Path
        loop_dir = tmp_path / "loop"
        shutil.copytree(Path("loops/assertion-density"), loop_dir)
        return loop_dir

    def test_a_real_run_records_ceilings_that_can_be_read_against_its_spend(
        self, tmp_path, capsys, monkeypatch,
    ):
        monkeypatch.setenv("BOUNDED_LOOPS_TRUST_STORE", str(tmp_path / "trust"))
        loop_dir = self._loop(tmp_path)

        assert main(["run", str(loop_dir), "--run-id", "e1", "--yes"]) == 0
        capsys.readouterr()
        assert main(["runs", str(loop_dir), "--show", "e1"]) == 0

        output = capsys.readouterr().out
        assert "attempts 1/10" in output, f"declared ceilings never reached the receipt: {output}"
        assert "gate: command (shipped)" in output

    def test_a_real_ceiling_halt_reports_the_bound_as_held_not_overrun(
        self, tmp_path, capsys, monkeypatch,
    ):
        """End to end: a gate that always fails, a ceiling of 2. The run writes THREE ledger rows
        and the receipt must say 2/2. This is the case the `attempted` field was introduced for and
        the first place anything actually reads it."""
        monkeypatch.setenv("BOUNDED_LOOPS_TRUST_STORE", str(tmp_path / "trust"))
        loop_dir = self._loop(tmp_path)

        main([
            "run", str(loop_dir), "--run-id", "h1", "--yes",
            "--max-iterations", "2", "--gate-override", "false",
        ])
        capsys.readouterr()
        assert main(["runs", str(loop_dir), "--show", "h1"]) == 0

        output = capsys.readouterr().out
        assert "attempts 2/2" in output, f"a bound that held exactly was misreported: {output}"

    def test_a_cli_override_is_recorded_as_the_ceiling_that_applied(
        self, tmp_path, capsys, monkeypatch,
    ):
        """bounds.yaml declares max_iterations: 10. With --max-iterations 3 the receipt must quote
        3 — the ceiling actually ENFORCED. A receipt quoting the YAML would name a bound that never
        applied, which is worse than naming none."""
        monkeypatch.setenv("BOUNDED_LOOPS_TRUST_STORE", str(tmp_path / "trust"))
        loop_dir = self._loop(tmp_path)

        main(["run", str(loop_dir), "--run-id", "o1", "--yes", "--max-iterations", "3"])
        capsys.readouterr()
        assert main(["runs", str(loop_dir), "--show", "o1"]) == 0

        output = capsys.readouterr().out
        assert "attempts 1/3" in output
        assert "/10" not in output, "the receipt quoted the YAML ceiling, not the enforced one"
