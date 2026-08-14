"""Global pytest safety fixtures for bounded-loops."""
from __future__ import annotations

from pathlib import Path

import pytest

from bounded_loops import trust_store


@pytest.fixture(autouse=True)
def _isolate_trust_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep all test-created trust records below pytest's temporary root."""

    test_store_path = tmp_path / ".bounded-loops" / "trust.json"
    temporary_home = tmp_path / "home"
    temporary_home.mkdir()
    monkeypatch.setenv("HOME", str(temporary_home))
    monkeypatch.setenv("USERPROFILE", str(temporary_home))
    monkeypatch.setenv("BOUNDED_LOOPS_TRUST_STORE", str(test_store_path))
    monkeypatch.setattr("bounded_loops.trust_store._DEFAULT_STORE_PATH", test_store_path)
    assert trust_store._store_path().is_relative_to(tmp_path)


@pytest.fixture(autouse=True)
def _isolate_project_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test-created `.bounded-loops/` workspace below pytest's temporary root.

    Since 0.6 `bl graph run --execute` defaults `--out` into the project workspace, which
    `workspace.discover()` resolves to the git repository root when nothing else is set. Without
    this fixture a test that omits `--out` writes a real run directory into THIS repository —
    which is how it was found. Tests that exercise discovery itself delete the variable.
    """
    monkeypatch.setenv("BOUNDED_LOOPS_WORKSPACE", str(tmp_path / "project"))
