"""Tests for ``bl loop new`` — scaffold a new loop package.

Verifies:
1. Each gate kind produces a directory that passes ``bl lint``.
2. Each gate kind produces a loop that reaches DONE via ``bl run --yes``.
3. The --dest flag controls destination correctly.
4. Bad names are rejected cleanly.
5. Existing destinations are refused.
6. bl loop with no action exits 1 with a clean message (no traceback).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_BL = [sys.executable, "-m", "bounded_loops.cli"]


def _bl(*args: str, cwd: Path | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    import os
    merged_env = {**os.environ}
    if env:
        merged_env.update(env)
    return subprocess.run(
        [*_BL, *args],
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
        env=merged_env,
    )


# ── Gate kind: command ───────────────────────────────────────────────────────

class TestLoopNewCommandGate:
    def test_scaffold_creates_directory(self, tmp_path):
        result = _bl("loop", "new", "my-loop", "--gate", "command",
                     "--dest", str(tmp_path / "my-loop"))
        assert result.returncode == 0, result.stderr
        assert (tmp_path / "my-loop").is_dir()

    def test_scaffold_produces_required_files(self, tmp_path):
        dest = tmp_path / "cmd-loop"
        _bl("loop", "new", "cmd-loop", "--gate", "command", "--dest", str(dest))
        for fname in ["loop.yaml", "bounds.yaml", "PROMPT.md", "STATE.md",
                      "cassettes/default.json"]:
            assert (dest / fname).exists(), f"Missing: {fname}"
        assert (dest / "seed" / "status.json").exists()
        assert (dest / "seed" / "check.py").exists()

    def test_scaffold_substitutes_loop_name(self, tmp_path):
        dest = tmp_path / "cmd-loop"
        _bl("loop", "new", "cmd-loop", "--gate", "command", "--dest", str(dest))
        content = (dest / "loop.yaml").read_text()
        assert "cmd-loop" in content
        assert "{{LOOP_NAME}}" not in content

    def test_scaffold_passes_bl_lint(self, tmp_path):
        dest = tmp_path / "cmd-loop"
        _bl("loop", "new", "cmd-loop", "--gate", "command", "--dest", str(dest))
        lint = _bl("lint", str(dest))
        assert lint.returncode == 0, lint.stderr + lint.stdout

    def test_scaffold_runs_to_done(self, tmp_path):
        """HARD REQUIREMENT: must reach DONE with no editing and no API key."""
        dest = tmp_path / "cmd-loop"
        _bl("loop", "new", "cmd-loop", "--gate", "command", "--dest", str(dest))
        trust_dir = tmp_path / "trust"
        trust_dir.mkdir()
        run = _bl("run", str(dest), "--yes",
                  env={"BOUNDED_LOOPS_TRUST_STORE": str(trust_dir),
                       "TMPDIR": "/tmp",
                       "XDG_CACHE_HOME": "/tmp/uvcache"})
        assert run.returncode == 0, (
            f"bl run failed (expected DONE).\nstdout: {run.stdout}\nstderr: {run.stderr}"
        )
        assert "DONE" in run.stdout


# ── Gate kind: pytest ────────────────────────────────────────────────────────

class TestLoopNewPytestGate:
    def test_scaffold_passes_bl_lint(self, tmp_path):
        dest = tmp_path / "py-loop"
        result = _bl("loop", "new", "py-loop", "--gate", "pytest",
                     "--dest", str(dest))
        assert result.returncode == 0, result.stderr
        lint = _bl("lint", str(dest))
        assert lint.returncode == 0, lint.stderr + lint.stdout

    def test_scaffold_runs_to_done(self, tmp_path):
        dest = tmp_path / "py-loop"
        _bl("loop", "new", "py-loop", "--gate", "pytest", "--dest", str(dest))
        trust_dir = tmp_path / "trust"
        trust_dir.mkdir()
        run = _bl("run", str(dest), "--yes",
                  env={"BOUNDED_LOOPS_TRUST_STORE": str(trust_dir),
                       "TMPDIR": "/tmp",
                       "XDG_CACHE_HOME": "/tmp/uvcache"})
        assert run.returncode == 0, (
            f"bl run --gate pytest failed.\nstdout: {run.stdout}\nstderr: {run.stderr}"
        )
        assert "DONE" in run.stdout


# ── Gate kind: jsonschema ────────────────────────────────────────────────────

class TestLoopNewJsonschemaGate:
    def test_scaffold_passes_bl_lint(self, tmp_path):
        dest = tmp_path / "js-loop"
        result = _bl("loop", "new", "js-loop", "--gate", "jsonschema",
                     "--dest", str(dest))
        assert result.returncode == 0, result.stderr
        lint = _bl("lint", str(dest))
        assert lint.returncode == 0, lint.stderr + lint.stdout

    def test_scaffold_runs_to_done(self, tmp_path):
        dest = tmp_path / "js-loop"
        _bl("loop", "new", "js-loop", "--gate", "jsonschema", "--dest", str(dest))
        trust_dir = tmp_path / "trust"
        trust_dir.mkdir()
        run = _bl("run", str(dest), "--yes",
                  env={"BOUNDED_LOOPS_TRUST_STORE": str(trust_dir),
                       "TMPDIR": "/tmp",
                       "XDG_CACHE_HOME": "/tmp/uvcache"})
        assert run.returncode == 0, (
            f"bl run --gate jsonschema failed.\nstdout: {run.stdout}\nstderr: {run.stderr}"
        )
        assert "DONE" in run.stdout


# ── Destination control ──────────────────────────────────────────────────────

class TestLoopNewDestination:
    def test_default_dest_is_name_under_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = _bl("loop", "new", "auto-dest-loop",
                     cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        assert (tmp_path / "auto-dest-loop").is_dir()

    def test_explicit_dest_is_exact_path(self, tmp_path):
        # --dest is the EXACT destination path (loop.yaml lands directly there)
        dest = tmp_path / "my-exact-dest"
        result = _bl("loop", "new", "named-loop", "--dest", str(dest))
        assert result.returncode == 0, result.stderr
        assert (dest).is_dir()
        # loop.yaml at dest root — name in yaml is "named-loop"
        content = (dest / "loop.yaml").read_text()
        assert "named-loop" in content

    def test_refuses_existing_destination(self, tmp_path):
        # --dest is the exact path; if that path already exists, refuse.
        dest = tmp_path / "my-loop"
        rc1 = _bl("loop", "new", "my-loop", "--dest", str(dest))
        assert rc1.returncode == 0, "first scaffold should succeed"
        assert dest.is_dir()
        # Second scaffold to the same exact path must fail.
        result = _bl("loop", "new", "my-loop", "--dest", str(dest))
        assert result.returncode != 0

    def test_run_scripts_are_executable(self, tmp_path):
        dest = tmp_path / "exec-test"
        _bl("loop", "new", "exec-test", "--gate", "command", "--dest", str(dest))
        assert (dest / "run.sh").stat().st_mode & 0o111


# ── Error cases ──────────────────────────────────────────────────────────────

class TestLoopNewErrors:
    def test_invalid_name_rejected(self, tmp_path):
        result = _bl("loop", "new", "../traversal", "--dest", str(tmp_path / "x"))
        assert result.returncode == 1
        combined = result.stdout + result.stderr
        assert "not a valid loop name" in combined

    def test_name_with_slash_rejected(self, tmp_path):
        result = _bl("loop", "new", "a/b", "--dest", str(tmp_path / "x"))
        assert result.returncode == 1

    def test_loop_no_action_exits_1(self):
        result = _bl("loop")
        assert result.returncode == 1
        combined = result.stdout + result.stderr
        assert "Traceback" not in combined

    def test_loop_new_default_gate_is_command(self, tmp_path):
        dest = tmp_path / "default-gate"
        _bl("loop", "new", "default-gate", "--dest", str(dest))
        content = (dest / "loop.yaml").read_text()
        assert "kind: command" in content
