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
