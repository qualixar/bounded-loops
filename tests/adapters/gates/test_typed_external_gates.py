from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bounded_loops.adapters.gates.gitleaks import GitleaksGate
from bounded_loops.adapters.gates.semgrep import SemgrepGate
from bounded_loops.adapters.gates.trivy import TrivyGate
from bounded_loops.adapters.gates.promptfoo import PromptfooGate
from bounded_loops.adapters.gates.great_expectations import GreatExpectationsGate
from bounded_loops.domain.models import LoopContext, Rung


def _ctx(tmp_path) -> LoopContext:
    return LoopContext(workspace=tmp_path, lap=1, rung=Rung.L1, trace_id="t")


def _proc(code=0, stdout="{}", stderr=""):
    return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)


def test_gitleaks_passes_on_empty_report(tmp_path):
    def fake_run(argv, **kwargs):
        report = Path(argv[argv.index("--report-path") + 1])
        report.write_text("[]", encoding="utf-8")
        return _proc(0, "")

    with patch("bounded_loops.adapters.gates.gitleaks.subprocess.run", side_effect=fake_run):
        verdict = GitleaksGate().check(_ctx(tmp_path))
    assert verdict.passed is True


def test_gitleaks_fails_on_findings(tmp_path):
    def fake_run(argv, **kwargs):
        report = Path(argv[argv.index("--report-path") + 1])
        report.write_text('[{"RuleID":"secret"}]', encoding="utf-8")
        return _proc(1, "")

    with patch("bounded_loops.adapters.gates.gitleaks.subprocess.run", side_effect=fake_run):
        verdict = GitleaksGate().check(_ctx(tmp_path))
    assert verdict.passed is False
    assert verdict.evidence["findings"] == 1


def test_semgrep_passes_with_empty_results(tmp_path):
    with patch("bounded_loops.adapters.gates.semgrep.subprocess.run", return_value=_proc(0, '{"results": []}')):
        verdict = SemgrepGate().check(_ctx(tmp_path))
    assert verdict.passed is True


def test_semgrep_fails_with_results(tmp_path):
    with patch("bounded_loops.adapters.gates.semgrep.subprocess.run", return_value=_proc(1, '{"results": [{"check_id":"x"}]}')):
        verdict = SemgrepGate().check(_ctx(tmp_path))
    assert verdict.passed is False


def test_trivy_counts_vulnerabilities(tmp_path):
    payload = '{"Results": [{"Vulnerabilities": [{"VulnerabilityID": "CVE-1"}]}]}'
    with patch("bounded_loops.adapters.gates.trivy.subprocess.run", return_value=_proc(1, payload)):
        verdict = TrivyGate().check(_ctx(tmp_path))
    assert verdict.passed is False
    assert verdict.evidence["findings"] == 1


def test_promptfoo_exit_zero_passes(tmp_path):
    with patch("bounded_loops.adapters.gates.promptfoo.subprocess.run", return_value=_proc(0, '{"stats": {}}')):
        verdict = PromptfooGate().check(_ctx(tmp_path))
    assert verdict.passed is True


def test_great_expectations_exit_one_fails(tmp_path):
    with patch("bounded_loops.adapters.gates.great_expectations.subprocess.run", return_value=_proc(1, "failed")):
        verdict = GreatExpectationsGate(checkpoint="demo").check(_ctx(tmp_path))
    assert verdict.passed is False