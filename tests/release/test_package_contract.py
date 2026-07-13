"""Release contracts exercised against the source tree and built metadata."""

from __future__ import annotations

import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _project() -> dict:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]


def test_default_install_includes_pytest_for_shipped_pytest_gates() -> None:
    dependencies = _project()["dependencies"]
    assert any(dependency.lower().startswith("pytest>=") for dependency in dependencies)


def test_next_release_is_minor_because_it_adds_public_cli_features() -> None:
    assert _project()["version"] == "0.3.0"


def test_pypi_project_urls_are_declared() -> None:
    urls = _project()["urls"]
    assert set(urls) >= {
        "Homepage",
        "Repository",
        "Documentation",
        "Changelog",
        "Issues",
    }
