"""Acceptance tests for CheckovGate."""
import json
import subprocess
from unittest.mock import patch, MagicMock

import pytest

from bounded_loops.adapters.gates.checkov import CheckovGate
from bounded_loops.domain.errors import GateError
from bounded_loops.domain.models import LoopContext, Rung


def _ctx(workspace):
    return LoopContext(workspace=workspace, lap=1, rung=Rung.L1, trace_id="t-checkov", env={})


def _mock_proc(returncode, stdout="", stderr=""):
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


def test_empty_stdout_raises_gate_error_not_false_pass(tmp_path):
    """Critical fix proof — the core defect: a crashed scanner
    (empty stdout, any exit code) must NEVER be treated as a clean pass."""
    with patch("subprocess.run", return_value=_mock_proc(0, stdout="")):
        with pytest.raises(GateError, match="produced no output"):
            CheckovGate().check(_ctx(tmp_path))
    with patch("subprocess.run", return_value=_mock_proc(1, stdout="   ")):
        with pytest.raises(GateError, match="produced no output"):
            CheckovGate().check(_ctx(tmp_path))


def test_genuinely_empty_parsed_report_raises_gate_error(tmp_path):
    """A real '[]'/'{}' (checkov ran but found
    NO recognized IaC) is not a clean pass for an IaC gate — it means the
    manifest is missing/gutted and the gate verified nothing. Fail CLOSED."""
    with patch("subprocess.run", return_value=_mock_proc(0, stdout="[]")):
        with pytest.raises(GateError, match="no recognized infrastructure-as-code"):
            CheckovGate().check(_ctx(tmp_path))


def test_real_flat_shape_zero_resources_raises_gate_error(tmp_path):
    """Verified live 2026-07-05 against real checkov 3.3.0: a directory with
    zero recognized IaC files produces this EXACT flat shape.
    resource_count=0 means nothing was scanned — gutting
    the anchor to non-IaC content must NOT green-light the gate. Fail CLOSED."""
    real_flat_shape = {"passed": 0, "failed": 0, "skipped": 0,
                        "parsing_errors": 0, "resource_count": 0,
                        "checkov_version": "3.3.0"}
    with patch("subprocess.run", return_value=_mock_proc(0, stdout=json.dumps(real_flat_shape))):
        with pytest.raises(GateError, match="ZERO infrastructure-as-code"):
            CheckovGate().check(_ctx(tmp_path))


def test_real_nonflat_shape_with_failures_fails(tmp_path):
    """Verified live 2026-07-05 against real checkov 3.3.0 scanning a
    deliberately-misconfigured Terraform file (open security group) —
    pinned as a fixture (trimmed to the fields this gate reads)."""
    real_shape_with_failures = {
        "check_type": "terraform",
        "results": {
            "failed_checks": [
                {"check_id": "CKV_AWS_24", "resource": "aws_security_group.wide_open"},
                {"check_id": "CKV_AWS_23", "resource": "aws_security_group.wide_open"},
            ],
            "parsing_errors": [],
        },
        "summary": {"passed": 5, "failed": 2, "skipped": 0, "parsing_errors": 0,
                     "resource_count": 2, "checkov_version": "3.3.0"},
        "url": "https://docs.prismacloud.io/",
    }
    with patch("subprocess.run", return_value=_mock_proc(1, stdout=json.dumps(real_shape_with_failures))):
        verdict = CheckovGate().check(_ctx(tmp_path))
    assert verdict.passed is False
    assert "CKV_AWS_24" in verdict.detail


def test_summary_failed_zero_passes_even_if_exit_code_nonzero(tmp_path):
    payload = {"summary": {"passed": 5, "failed": 0}, "results": {"failed_checks": []}}
    with patch("subprocess.run", return_value=_mock_proc(1, stdout=json.dumps(payload))):
        verdict = CheckovGate().check(_ctx(tmp_path))
    assert verdict.passed is True


def test_summary_failed_nonzero_fails_with_summarized_detail(tmp_path):
    payload = {
        "summary": {"passed": 2, "failed": 1},
        "results": {"failed_checks": [{"check_id": "CKV_AWS_20", "resource": "aws_s3_bucket.public"}]},
    }
    with patch("subprocess.run", return_value=_mock_proc(1, stdout=json.dumps(payload))):
        verdict = CheckovGate().check(_ctx(tmp_path))
    assert verdict.passed is False
    assert "CKV_AWS_20" in verdict.detail
    assert "aws_s3_bucket.public" in verdict.detail


def test_list_shaped_payload_multi_framework_is_normalized(tmp_path):
    payload = [
        {"summary": {"passed": 1, "failed": 0}, "results": {"failed_checks": []}},
        {"summary": {"passed": 0, "failed": 1}, "results": {"failed_checks": [{"check_id": "CKV_K8S_1", "resource": "pod.x"}]}},
    ]
    with patch("subprocess.run", return_value=_mock_proc(1, stdout=json.dumps(payload))):
        verdict = CheckovGate().check(_ctx(tmp_path))
    assert verdict.passed is False
    assert "CKV_K8S_1" in verdict.detail


def test_parsing_errors_with_zero_failed_checks_raises_gate_error(tmp_path):
    payload = {"summary": {"passed": 0, "failed": 0}, "results": {"failed_checks": [], "parsing_errors": ["bad.tf"]}}
    with patch("subprocess.run", return_value=_mock_proc(1, stdout=json.dumps(payload))):
        with pytest.raises(GateError, match="parsing error"):
            CheckovGate().check(_ctx(tmp_path))


def test_summary_level_parsing_errors_int_also_raises_gate_error(tmp_path):
    """Fix proof — the unverified-location gap: checks BOTH
    plausible parsing-error locations, not just results.parsing_errors."""
    payload = {"summary": {"passed": 0, "failed": 0, "parsing_errors": 2}, "results": {"failed_checks": []}}
    with patch("subprocess.run", return_value=_mock_proc(1, stdout=json.dumps(payload))):
        with pytest.raises(GateError, match="parsing error"):
            CheckovGate().check(_ctx(tmp_path))


def test_type_confused_summary_failed_does_not_crash(tmp_path):
    """Fix proof — non-int summary.failed (string,
    bool, object) must not raise ValueError/TypeError. It now degrades to a
    clean GateError, not a crash: the garbage `failed` coerces to 0 AND zero
    resources were scanned (passed=0, resource_count absent), so the gate fails
    CLOSED on nothing-to-verify — never a false pass, never
    an uncaught exception."""
    for bad_value in ["not-a-number", True, {"nested": "object"}, [1, 2]]:
        payload = {"summary": {"passed": 0, "failed": bad_value}, "results": {"failed_checks": []}}
        with patch("subprocess.run", return_value=_mock_proc(1, stdout=json.dumps(payload))):
            with pytest.raises(GateError):   # clean fail-closed, not a crash
                CheckovGate().check(_ctx(tmp_path))


def test_non_dict_report_entries_do_not_crash(tmp_path):
    """Fix proof — type-confused results/summary at any nesting
    level degrades gracefully instead of raising AttributeError."""
    # BOTH summary and results are garbage (non-dict) and there is no flat
    # `failed` count — the report yields NO interpretable signal at all. The
    # gate must fail CLOSED here (GateError), not silently pass by extracting
    # nothing. (this previously returned passed=True — a real
    # fail-open on uninterpretable output.)
    payload = [{"summary": "not-a-dict", "results": "also-not-a-dict"}]
    with patch("subprocess.run", return_value=_mock_proc(1, stdout=json.dumps(payload))):
        with pytest.raises(GateError):
            CheckovGate().check(_ctx(tmp_path))


def test_unparseable_output_raises_gate_error(tmp_path):
    with patch("subprocess.run", return_value=_mock_proc(1, stdout="not json")):
        with pytest.raises(GateError, match="could not parse"):
            CheckovGate().check(_ctx(tmp_path))


def test_missing_binary_raises_gate_error(tmp_path):
    with patch("subprocess.run", side_effect=FileNotFoundError("checkov not found")):
        with pytest.raises(GateError, match="could not launch checkov"):
            CheckovGate().check(_ctx(tmp_path))


def test_timeout_raises_gate_error(tmp_path):
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="checkov", timeout=1)):
        with pytest.raises(GateError, match="timed out"):
            CheckovGate(timeout_s=1).check(_ctx(tmp_path))


def test_env_is_allowlisted_not_full_parent(tmp_path, monkeypatch):
    monkeypatch.setenv("CHECKOV_GATE_TEST_SECRET", "top-secret")
    captured = {}

    # Return a real report that DID scan a resource and passed, so check()
    # completes (rather than fail-closing on nothing-scanned) and we can
    # inspect the env it built.
    passing = {"passed": 1, "failed": 0, "skipped": 0, "parsing_errors": 0,
               "resource_count": 1, "checkov_version": "3.3.0"}

    def fake_run(*args, **kwargs):
        captured["env"] = kwargs.get("env", {})
        return _mock_proc(0, stdout=json.dumps(passing))

    with patch("subprocess.run", side_effect=fake_run):
        CheckovGate().check(_ctx(tmp_path))
    assert "CHECKOV_GATE_TEST_SECRET" not in captured["env"]


@pytest.mark.skipif(
    __import__("shutil").which("checkov") is None,
    reason="checkov not installed on this machine",
)
def test_real_checkov_on_a_clean_workspace(tmp_path):
    """Real, unmocked proof: a workspace with no IaC files
    scans zero resources, so the gate fails CLOSED with a GateError rather than
    reporting a false clean pass on nothing-verified."""
    with pytest.raises(GateError):
        CheckovGate().check(_ctx(tmp_path))
