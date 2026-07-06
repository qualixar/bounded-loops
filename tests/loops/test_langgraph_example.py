"""Acceptance tests for loops/langgraph-example/ (pattern shared by the other framework-example loops)."""
import subprocess
import sys
import pytest
from pathlib import Path

LOOP_DIR = Path(__file__).parent.parent.parent / "loops" / "langgraph-example"


def test_bl_lint_passes_without_framework_installed():
    """Manifest validation never imports the framework — must pass in CI
    even when langgraph isn't installed."""
    result = subprocess.run([sys.executable, "-m", "bounded_loops.cli", "lint", str(LOOP_DIR)], capture_output=True, text=True)
    assert result.returncode == 0


def test_manifest_uses_python_callable_runner():
    import yaml
    manifest = yaml.safe_load((LOOP_DIR / "loop.yaml").read_text())
    assert manifest["runner"]["default"] == "python_callable"


def test_glue_module_path_resolves_to_a_real_file():
    """The module_path in loop.yaml must point at a glue.py that actually
    exists — catches a stale/typo'd path without needing the framework.

    Fix: the original assertion had an `or (LOOP_DIR / "glue.py").exists()`
    fallback that made the test unable to ever fail — verified concretely
    (module_path="loops.langgraph-example.glue" against LOOP_DIR already ending
    in .../loops/langgraph-example/ produces a doubled, never-existing path via
    the first operand, so the OR always fell through to the second regardless
    of whether module_path was correct). A typo'd module_path silently passed
    this test and only surfaced later at the framework-gated run test, which
    most CI skips. Fixed: resolve module_path relative to the REPO ROOT (it is
    a dotted import path from repo root, per PythonCallableRunner's
    importlib.import_module usage), with no fallback."""
    import yaml
    REPO_ROOT = LOOP_DIR.parent.parent  # tests/loops/../.. == repo root
    manifest = yaml.safe_load((LOOP_DIR / "loop.yaml").read_text())
    module_path = manifest["runner"]["module_path"]
    assert (REPO_ROOT / (module_path.replace(".", "/") + ".py")).exists()


def test_run_only_with_framework_installed(tmp_path):
    """The real end-to-end proof — SKIPPED, not failed, if langgraph isn't
    installed. This is the test that actually proves the copy-paste payload
    works, for a CI runner that opts in with the framework installed."""
    pytest.importorskip("langgraph")
    result = subprocess.run([sys.executable, "-m", "bounded_loops.cli", "run", str(LOOP_DIR), "--yes"],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0
    assert "DONE" in result.stdout
