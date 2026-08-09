"""Regression tests for the deterministic, hermetic pytest contract."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest

from bounded_loops.trust_store import _store_path


REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.external_tool
def test_external_tool_collection_sentinel() -> None:
    """This test must never be part of the default pytest lane."""


def test_every_pytest_test_gets_a_temporary_trust_store(tmp_path: Path) -> None:
    configured = os.environ.get("BOUNDED_LOOPS_TRUST_STORE")

    assert configured is not None
    assert Path(configured).is_relative_to(tmp_path)


def test_fallback_trust_store_stays_within_the_test_temporary_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BOUNDED_LOOPS_TRUST_STORE", raising=False)

    assert _store_path().is_relative_to(tmp_path)


def test_production_fallback_writes_only_to_a_controlled_home(tmp_path: Path) -> None:
    controlled_home = tmp_path / "child-home"
    controlled_home.mkdir()
    loop_dir = tmp_path / "child-loop"
    loop_dir.mkdir()
    environment = os.environ.copy()
    environment.pop("BOUNDED_LOOPS_TRUST_STORE", None)
    environment["HOME"] = str(controlled_home)
    environment["USERPROFILE"] = str(controlled_home)
    environment["F0_LOOP_DIR"] = str(loop_dir)
    script = """
from os import environ
from pathlib import Path
from bounded_loops.trust_store import record_trust, _store_path

loop_dir = Path(environ['F0_LOOP_DIR'])
record_trust(loop_dir, 'true')
print(_store_path())
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    expected_store = controlled_home / ".bounded-loops" / "trust.json"
    assert result.returncode == 0, result.stderr
    assert expected_store.is_file()
    assert str(expected_store) in result.stdout


def test_default_collection_excludes_external_tool_sentinel() -> None:
    checkov_test = REPO_ROOT / "tests" / "adapters" / "gates" / "test_checkov.py"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            __file__,
            str(checkov_test),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "test_external_tool_collection_sentinel" not in result.stdout
    assert "test_convergence_demo_fails_twice_then_passes_on_lap_three" not in result.stdout
    assert "test_real_checkov_on_a_clean_workspace" not in result.stdout


def test_pyproject_declares_all_execution_lane_markers() -> None:
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    markers = config["tool"]["pytest"]["ini_options"]["markers"]

    for marker in ("network", "external_tool", "provider_smoke", "clean_install"):
        assert any(entry.startswith(f"{marker}:") for entry in markers)
