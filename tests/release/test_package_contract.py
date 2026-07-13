"""Release contracts exercised against the source tree and built metadata."""

from __future__ import annotations

import json
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


def test_readme_puts_verified_quick_start_above_the_fold() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    lines = readme.splitlines()
    first_install = next(i for i, line in enumerate(lines, 1) if "pip install" in line)
    assert first_install <= 40
    assert len(readme.split()) <= 2200
    assert "actions/workflows/ci.yml/badge.svg" in readme
    assert "tests-678_passing" not in readme


def test_hero_demo_is_committed_and_regenerable() -> None:
    assert (REPO_ROOT / "assets" / "demo.gif").is_file()
    assert (REPO_ROOT / "assets" / "demo.tape").is_file()


def test_codex_plugin_uses_current_manifest_contract() -> None:
    plugin_root = REPO_ROOT / "plugins" / "codex"
    manifest = json.loads(
        (plugin_root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    assert manifest["name"] == "bounded-loops"
    assert manifest["version"] == "0.3.0"
    assert manifest["skills"] == "./skills/"
    assert manifest["mcpServers"] == "./.mcp.json"
    assert not (plugin_root / "plugin.toml").exists()


def test_claude_plugin_has_a_package_manifest() -> None:
    manifest = json.loads(
        (
            REPO_ROOT
            / "plugins"
            / "claude-code"
            / ".claude-plugin"
            / "plugin.json"
        ).read_text(encoding="utf-8")
    )
    assert manifest["name"] == "bounded-loops"
    assert manifest["version"] == "0.3.0"


def test_plugin_installation_and_mcp_extra_are_documented() -> None:
    text = (REPO_ROOT / "plugins" / "README.md").read_text(encoding="utf-8")
    assert 'pip install "bounded-loops[mcp]"' in text
    assert "claude plugin" in text
    assert "codex plugin" in text
    assert "bounded-loops-mcp" in text


def test_clean_room_release_gate_is_wired_into_ci() -> None:
    script = REPO_ROOT / "scripts" / "verify_clean_room.py"
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    assert script.is_file()
    assert "clean-room" in workflow
    assert "verify_clean_room.py" in workflow


def test_real_codex_example_is_a_machine_readable_receipt() -> None:
    example = REPO_ROOT / "docs" / "real-run-example"
    ledger_lines = (example / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
    transcript_lines = (
        (example / "transcript.jsonl").read_text(encoding="utf-8").splitlines()
    )
    ledger = [json.loads(line) for line in ledger_lines]
    transcript = [json.loads(line) for line in transcript_lines]

    assert ledger[-1]["decision"] == "done"
    assert ledger[-1]["verdict"]["passed"] is True
    assert ledger[-1]["budget_spent"]["tokens"] > 0
    assert any(event.get("type") == "turn.completed" for event in transcript)
