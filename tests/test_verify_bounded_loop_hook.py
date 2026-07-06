"""Acceptance tests for bounded_loops/hooks/verify_bounded_loop.py."""
import json
import sys

from bounded_loops.hooks.verify_bounded_loop import main, _check, _extract_cwd, _validate_cwd
from bounded_loops.trust_store import record_trust


def test_no_loop_yaml_allows(tmp_path):
    passed, reason = _check(tmp_path)
    assert passed is True
    assert "nothing to verify" in reason


def test_unsupported_gate_kind_allows(tmp_path):
    (tmp_path / "loop.yaml").write_text("gate:\n  kind: jsonschema\n")
    passed, reason = _check(tmp_path)
    assert passed is True
    assert "not checked by this hook" in reason


def test_untrusted_loop_allows_without_executing(tmp_path):
    """Fix proof — THE core confirmation-bypass fix. A loop with a
    real, currently-failing gate command must still ALLOW if it has never
    been trusted, proving the hook never auto-executes unreviewed shell."""
    (tmp_path / "loop.yaml").write_text('gate:\n  kind: command\n  run: "false"\n')
    passed, reason = _check(tmp_path)
    assert passed is True
    assert "not yet trusted" in reason


def test_trusted_passing_gate_allows(tmp_path, monkeypatch):
    monkeypatch.setenv("BOUNDED_LOOPS_TRUST_STORE", str(tmp_path / "trust.json"))
    (tmp_path / "loop.yaml").write_text('gate:\n  kind: command\n  run: "true"\n')
    record_trust(tmp_path, "true")
    passed, reason = _check(tmp_path)
    assert passed is True


def test_trusted_failing_gate_blocks(tmp_path, monkeypatch):
    """The real, load-bearing test: a currently-failing, ALREADY-TRUSTED
    gate must block, not silently allow session-stop."""
    monkeypatch.setenv("BOUNDED_LOOPS_TRUST_STORE", str(tmp_path / "trust.json"))
    (tmp_path / "loop.yaml").write_text('gate:\n  kind: command\n  run: "false"\n')
    record_trust(tmp_path, "false")
    passed, reason = _check(tmp_path)
    assert passed is False
    assert "still fails" in reason


def test_trust_is_specific_to_the_exact_command(tmp_path, monkeypatch):
    """Trust for one gate command does NOT carry over to a different one in
    the same directory — proves the composite key, not just the directory."""
    monkeypatch.setenv("BOUNDED_LOOPS_TRUST_STORE", str(tmp_path / "trust.json"))
    (tmp_path / "loop.yaml").write_text('gate:\n  kind: command\n  run: "false"\n')
    record_trust(tmp_path, "some other command")  # different command, same dir
    passed, reason = _check(tmp_path)
    assert passed is True
    assert "not yet trusted" in reason


def test_child_cannot_see_unallowlisted_env_var(tmp_path, monkeypatch):
    """Fix proof — the full-env-inheritance leak is closed.

    The gate FAILS (exit 1) so its stdout is surfaced in `reason` (a passing
    gate's stdout is discarded). shell=False does no $VAR expansion,
    so the env var is read via a real script that emits the value (or ABSENT)
    and exits 1 — proving the child's env was scrubbed to the allowlist.
    """
    monkeypatch.setenv("BOUNDED_LOOPS_TRUST_STORE", str(tmp_path / "trust.json"))
    monkeypatch.setenv("BOUNDED_LOOPS_TEST_SECRET", "top-secret")
    script = tmp_path / "leak_probe.py"
    script.write_text(
        "import os, sys\n"
        "sys.stdout.write(os.environ.get('BOUNDED_LOOPS_TEST_SECRET', 'ABSENT'))\n"
        "sys.exit(1)\n"
    )
    cmd = f"python3 {script}"
    (tmp_path / "loop.yaml").write_text(f'gate:\n  kind: command\n  run: "{cmd}"\n')
    record_trust(tmp_path, cmd)
    passed, reason = _check(tmp_path)
    assert passed is False
    assert "top-secret" not in reason
    assert "ABSENT" in reason


def test_pytest_no_tests_collected_blocks_missing_anchor(tmp_path, monkeypatch):
    """Fix: exit 5 (no tests collected) from a trusted pytest
    loop means the verification ANCHOR is gone (test files deleted) — the exact
    'talk past the gate' case this hook exists to catch. It must BLOCK
    session-stop, matching the engine's PytestGate (which raises GateError on
    exit 5), NOT allow it like a clean pass."""
    monkeypatch.setenv("BOUNDED_LOOPS_TRUST_STORE", str(tmp_path / "trust.json"))
    (tmp_path / "loop.yaml").write_text("gate:\n  kind: pytest\n")
    record_trust(tmp_path, "pytest -q")
    passed, reason = _check(tmp_path)
    assert passed is False
    assert "collected NO tests" in reason or "anchor" in reason


def test_extract_cwd_claude_code_and_codex_use_cwd_field():
    assert _extract_cwd({"cwd": "/x"}, "claude-code") == "/x"
    assert _extract_cwd({"cwd": "/x"}, "codex") == "/x"


def test_extract_cwd_antigravity_uses_workspace_paths_first_entry():
    assert _extract_cwd({"workspacePaths": ["/a", "/b"]}, "antigravity") == "/a"


def test_extract_cwd_missing_field_returns_none():
    assert _extract_cwd({}, "claude-code") is None


def test_extract_cwd_unknown_tool_returns_none():
    assert _extract_cwd({"cwd": "/x"}, "some-other-tool") is None


def test_validate_cwd_rejects_relative_path():
    assert _validate_cwd("relative/path") is None


def test_validate_cwd_rejects_nonexistent_path(tmp_path):
    assert _validate_cwd(str(tmp_path / "does-not-exist")) is None


def test_validate_cwd_rejects_symlink(tmp_path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real_dir)
    assert _validate_cwd(str(link)) is None


def test_validate_cwd_accepts_real_absolute_directory(tmp_path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    assert _validate_cwd(str(real_dir)) == real_dir.resolve()


def test_main_claude_code_trusted_failing_gate_exits_2_with_stderr(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("BOUNDED_LOOPS_TRUST_STORE", str(tmp_path / "trust.json"))
    (tmp_path / "loop.yaml").write_text('gate:\n  kind: command\n  run: "false"\n')
    record_trust(tmp_path, "false")
    monkeypatch.setattr(sys, "stdin", type("F", (), {"read": lambda self: json.dumps({"cwd": str(tmp_path)})})())
    code = main(["verify_bounded_loop.py", "claude-code"])
    assert code == 2
    assert "still fails" in capsys.readouterr().err


def test_main_malformed_stdin_fails_open_allows(tmp_path, monkeypatch):
    """Fix: a malformed hook payload (not JSON) must fail OPEN
    (exit 0 = allow), consistent with every other guard in this hook — never
    crash with an uncaught JSONDecodeError (undocumented exit 1)."""
    monkeypatch.setattr(sys, "stdin", type("F", (), {"read": lambda self: "not json at all"})())
    code = main(["verify_bounded_loop.py", "claude-code"])
    assert code == 0


def test_main_claude_code_trusted_passing_gate_exits_0(tmp_path, monkeypatch):
    monkeypatch.setenv("BOUNDED_LOOPS_TRUST_STORE", str(tmp_path / "trust.json"))
    (tmp_path / "loop.yaml").write_text('gate:\n  kind: command\n  run: "true"\n')
    record_trust(tmp_path, "true")
    monkeypatch.setattr(sys, "stdin", type("F", (), {"read": lambda self: json.dumps({"cwd": str(tmp_path)})})())
    code = main(["verify_bounded_loop.py", "claude-code"])
    assert code == 0


def test_main_antigravity_trusted_failing_gate_prints_deny_json(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("BOUNDED_LOOPS_TRUST_STORE", str(tmp_path / "trust.json"))
    (tmp_path / "loop.yaml").write_text('gate:\n  kind: command\n  run: "false"\n')
    record_trust(tmp_path, "false")
    monkeypatch.setattr(sys, "stdin", type("F", (), {"read": lambda self: json.dumps({"workspacePaths": [str(tmp_path)]})})())
    code = main(["verify_bounded_loop.py", "antigravity"])
    out = json.loads(capsys.readouterr().out)
    assert out["decision"] == "deny"
    assert code == 1   # pin the exact Antigravity deny contract, not just "nonzero"


def test_main_antigravity_trusted_passing_gate_prints_allow_json(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("BOUNDED_LOOPS_TRUST_STORE", str(tmp_path / "trust.json"))
    (tmp_path / "loop.yaml").write_text('gate:\n  kind: command\n  run: "true"\n')
    record_trust(tmp_path, "true")
    monkeypatch.setattr(sys, "stdin", type("F", (), {"read": lambda self: json.dumps({"workspacePaths": [str(tmp_path)]})})())
    code = main(["verify_bounded_loop.py", "antigravity"])
    out = json.loads(capsys.readouterr().out)
    assert out["decision"] == "allow"
    assert code == 0


def test_main_missing_cwd_field_allows_without_guessing(monkeypatch):
    monkeypatch.setattr(sys, "stdin", type("F", (), {"read": lambda self: "{}"})())
    assert main(["verify_bounded_loop.py", "claude-code"]) == 0


def test_main_invalid_cwd_allows_without_guessing(monkeypatch):
    """A cwd that fails _validate_cwd (e.g. relative path from a malformed
    payload) must allow, not raise or guess-execute."""
    monkeypatch.setattr(
        sys, "stdin",
        type("F", (), {"read": lambda self: json.dumps({"cwd": "relative/path"})})(),
    )
    assert main(["verify_bounded_loop.py", "claude-code"]) == 0


def test_main_defaults_to_claude_code_tool_when_argv_missing(monkeypatch):
    """argv[1] absent → defaults to claude-code per the docstring contract,
    exercised via the exit-code protocol rather than the JSON one."""
    monkeypatch.setattr(sys, "stdin", type("F", (), {"read": lambda self: "{}"})())
    assert main(["verify_bounded_loop.py"]) == 0


def test_main_empty_stdin_treated_as_empty_json(monkeypatch):
    monkeypatch.setattr(sys, "stdin", type("F", (), {"read": lambda self: ""})())
    assert main(["verify_bounded_loop.py", "claude-code"]) == 0
