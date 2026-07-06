"""Acceptance tests for loops/crewai-example/ (pattern shared with the other framework-example loops)."""
import subprocess
import sys
import pytest
from pathlib import Path

LOOP_DIR = Path(__file__).parent.parent.parent / "loops" / "crewai-example"


def test_bl_lint_passes_without_framework_installed():
    """Manifest validation never imports the framework — must pass in CI
    even when crewai isn't installed."""
    result = subprocess.run([sys.executable, "-m", "bounded_loops.cli", "lint", str(LOOP_DIR)], capture_output=True, text=True)
    assert result.returncode == 0


def test_manifest_uses_python_callable_runner():
    import yaml
    manifest = yaml.safe_load((LOOP_DIR / "loop.yaml").read_text())
    assert manifest["runner"]["default"] == "python_callable"


def test_glue_module_path_resolves_to_a_real_file():
    """The module_path in loop.yaml must point at a glue.py that actually
    exists — catches a stale/typo'd path without needing the framework.

    Fix (see test_langgraph_example.py for the full rationale):
    resolve module_path relative to the REPO ROOT (it is a dotted import
    path from repo root, per PythonCallableRunner's importlib.import_module
    usage), with no fallback that would let a typo'd path
    silently pass."""
    import yaml
    REPO_ROOT = LOOP_DIR.parent.parent  # tests/loops/../.. == repo root
    manifest = yaml.safe_load((LOOP_DIR / "loop.yaml").read_text())
    module_path = manifest["runner"]["module_path"]
    assert (REPO_ROOT / (module_path.replace(".", "/") + ".py")).exists()


def test_run_only_with_framework_installed(tmp_path):
    """The real end-to-end proof — SKIPPED, not failed, if crewai isn't
    installed. This is the test that actually proves the copy-paste payload
    works, for a CI runner that opts in with the framework installed."""
    pytest.importorskip("crewai")
    result = subprocess.run([sys.executable, "-m", "bounded_loops.cli", "run", str(LOOP_DIR), "--yes"],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0
    assert "DONE" in result.stdout
