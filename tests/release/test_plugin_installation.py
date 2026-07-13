"""Smoke-test the native Codex plugin without touching the real Codex home."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_codex_marketplace_install_in_isolated_home(tmp_path: Path) -> None:
    codex = shutil.which("codex")
    if codex is None:
        pytest.skip("Codex CLI is not installed")

    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)

    added = subprocess.run(
        [codex, "plugin", "marketplace", "add", str(REPO_ROOT), "--json"],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert json.loads(added.stdout)["marketplaceName"] == "bounded-loops"

    installed = subprocess.run(
        [codex, "plugin", "add", "bounded-loops@bounded-loops", "--json"],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    payload = json.loads(installed.stdout)
    assert payload["version"] == "0.3.1"
    installed_path = Path(payload["installedPath"])
    assert (installed_path / ".codex-plugin" / "plugin.json").is_file()
    assert (installed_path / ".mcp.json").is_file()
    assert (installed_path / "skills" / "bounded-loops" / "SKILL.md").is_file()
