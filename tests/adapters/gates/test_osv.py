"""Acceptance tests for OsvGate."""
import json
import subprocess
from unittest.mock import patch, MagicMock

import pytest

from bounded_loops.adapters.gates.osv import OsvGate
from bounded_loops.domain.errors import GateError
from bounded_loops.domain.models import LoopContext, Rung


def _ctx(workspace):
    return LoopContext(workspace=workspace, lap=1, rung=Rung.L1, trace_id="t-osv", env={})


def _mock_proc(returncode, stdout="", stderr=""):
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


def test_exit_0_passes(tmp_path):
    # Real osv-scanner emits its JSON report even on a clean scan (verified
    # live 2026-07-06: exit 0 -> {"results": [], ...}), NOT an empty object.
    with patch("subprocess.run", return_value=_mock_proc(0, stdout='{"results": []}')):
        verdict = OsvGate().check(_ctx(tmp_path))
    assert verdict.passed is True


def test_exit_0_with_empty_stdout_raises_not_false_pass(tmp_path):
    """Security: a scanner that exits 0 but produced no real report (shadowed
    on PATH, --soft-fail-wrapped, no-op stub, crashed-after-fork) must NOT be
    reported as a clean PASS — it must fail closed with GateError."""
    with patch("subprocess.run", return_value=_mock_proc(0, stdout="")):
        with pytest.raises(GateError):
            OsvGate().check(_ctx(tmp_path))


def test_exit_0_with_garbage_stdout_raises_not_false_pass(tmp_path):
    """Same guard, non-empty-but-not-osv-JSON output (e.g. a wrapper that
    printed its own banner and exited 0)."""
    with patch("subprocess.run", return_value=_mock_proc(0, stdout="SEGFAULT (core dumped)")):
        with pytest.raises(GateError):
            OsvGate().check(_ctx(tmp_path))


def test_exit_128_no_packages_found_raises_gate_error(tmp_path):
    """Exit 128 ('no packages found') is NOT a
    pass. For a dependency-vulnerability gate it means the manifest is missing
    or was gutted (a cassette overwriting package-lock.json with `{}` triggers
    exactly this) — the gate verified nothing. Fail CLOSED (GateError)."""
    with patch("subprocess.run", return_value=_mock_proc(128, stderr="no packages found")):
        with pytest.raises(GateError, match="NO packages"):
            OsvGate().check(_ctx(tmp_path))


def test_exit_1_fails_with_summarized_detail(tmp_path):
    payload = {"results": [{"packages": [{"package": {"name": "left-pad", "version": "1.0.0"},
                                            "vulnerabilities": [{"id": "GHSA-xxxx-yyyy-zzzz"}]}]}]}
    with patch("subprocess.run", return_value=_mock_proc(1, stdout=json.dumps(payload))):
        verdict = OsvGate().check(_ctx(tmp_path))
    assert verdict.passed is False
    assert "left-pad@1.0.0" in verdict.detail
    assert "GHSA-xxxx-yyyy-zzzz" in verdict.detail


def test_exit_1_with_malformed_json_falls_back_to_raw_tail_not_false_claim(tmp_path):
    """Fix proof: must NOT say 'no known vulnerabilities' on exit 1
    (self-contradicting) — falls back to the raw tail instead."""
    with patch("subprocess.run", return_value=_mock_proc(1, stdout="not json")):
        verdict = OsvGate().check(_ctx(tmp_path))
    assert verdict.passed is False
    assert "no known vulnerabilities" not in verdict.detail


def test_exit_1_with_adversarially_shaped_valid_json_does_not_crash(tmp_path):
    """Fix proof — critical finding: valid JSON, wrong nested types
    (package as a string, vulnerabilities as a scalar) must not raise."""
    payload = {"results": [{"packages": ["not-a-dict"]}]}
    with patch("subprocess.run", return_value=_mock_proc(1, stdout=json.dumps(payload))):
        verdict = OsvGate().check(_ctx(tmp_path))   # must not raise
    assert verdict.passed is False


def test_unexpected_exit_code_raises_gate_error(tmp_path):
    with patch("subprocess.run", return_value=_mock_proc(127, stderr="command not found")):
        with pytest.raises(GateError, match="unexpected code 127"):
            OsvGate().check(_ctx(tmp_path))


def test_missing_binary_raises_gate_error_not_crash(tmp_path):
    with patch("subprocess.run", side_effect=FileNotFoundError("osv-scanner not found")):
        with pytest.raises(GateError, match="could not launch osv-scanner"):
            OsvGate().check(_ctx(tmp_path))


def test_timeout_raises_gate_error(tmp_path):
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="osv-scanner", timeout=1)):
        with pytest.raises(GateError, match="timed out"):
            OsvGate(timeout_s=1).check(_ctx(tmp_path))


def test_env_is_allowlisted_not_full_parent(tmp_path, monkeypatch):
    monkeypatch.setenv("OSV_GATE_TEST_SECRET", "top-secret")
    captured = {}

    def fake_run(*args, **kwargs):
        captured["env"] = kwargs.get("env", {})
        return _mock_proc(0, stdout='{"results": []}')

    with patch("subprocess.run", side_effect=fake_run):
        OsvGate().check(_ctx(tmp_path))
    assert "OSV_GATE_TEST_SECRET" not in captured["env"]


@pytest.mark.skipif(
    __import__("shutil").which("osv-scanner") is None,
    reason="osv-scanner not installed on this machine",
)
def test_real_osv_scanner_on_a_clean_workspace(tmp_path):
    """Real, unmocked proof: a workspace with no manifest/
    lockfile hits the REAL exit code 128 (no packages found). The gate could
    not verify any dependencies, so it fails CLOSED with a GateError rather
    than reporting a false clean pass."""
    with pytest.raises(GateError, match="NO packages"):
        OsvGate().check(_ctx(tmp_path))
