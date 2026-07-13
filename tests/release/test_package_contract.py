"""Release contracts exercised against the source tree and built metadata."""

from __future__ import annotations

import json
import struct
import tomllib
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]


def _project() -> dict:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]


def test_default_install_includes_pytest_for_shipped_pytest_gates() -> None:
    dependencies = _project()["dependencies"]
    assert any(dependency.lower().startswith("pytest>=") for dependency in dependencies)


def test_patch_release_contains_version_probe_fix() -> None:
    assert _project()["version"] == "0.3.1"


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


def test_root_readme_contains_no_machine_terminal_transcript() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "Last login:" not in readme
    assert "/Users/" not in readme
    assert "Codex CLI 0.144.3 installed successfully" not in readme


def test_hero_demo_is_committed_and_regenerable() -> None:
    assert (REPO_ROOT / "assets" / "demo.gif").is_file()
    assert (REPO_ROOT / "assets" / "demo.tape").is_file()


def test_social_preview_has_github_recommended_dimensions() -> None:
    preview = REPO_ROOT / "assets" / "social-preview.png"
    renderer = REPO_ROOT / "scripts" / "render_social_preview.py"
    assert preview.is_file()
    assert renderer.is_file()
    payload = preview.read_bytes()
    assert payload.startswith(b"\x89PNG\r\n\x1a\n")
    assert struct.unpack(">II", payload[16:24]) == (1280, 640)


def test_codex_plugin_uses_current_manifest_contract() -> None:
    plugin_root = REPO_ROOT / "plugins" / "codex"
    manifest = json.loads(
        (plugin_root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    assert manifest["name"] == "bounded-loops"
    assert manifest["version"] == "0.3.1"
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
    assert manifest["version"] == "0.3.1"


def test_plugin_installation_and_mcp_extra_are_documented() -> None:
    text = (REPO_ROOT / "plugins" / "README.md").read_text(encoding="utf-8")
    assert 'pip install "bounded-loops[mcp]"' in text
    assert "claude plugin" in text
    assert "codex plugin" in text
    assert "bounded-loops-mcp" in text


def test_clean_room_release_gate_is_wired_into_ci() -> None:
    script = REPO_ROOT / "scripts" / "verify_clean_room.py"
    readme_script = REPO_ROOT / "scripts" / "verify_readme_outputs.py"
    mcp_script = REPO_ROOT / "scripts" / "smoke_mcp_server.py"
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    assert script.is_file()
    assert readme_script.is_file()
    assert mcp_script.is_file()
    assert "clean-room" in workflow
    assert "python -m pip install build ." in workflow
    assert "verify_clean_room.py" in workflow
    assert "verify_readme_outputs.py" in workflow


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


def test_release_metadata_uses_the_canonical_catalog_count_and_version() -> None:
    loop_dirs = sorted((REPO_ROOT / "loops").glob("*/loop.yaml"))
    framework_loops = {
        "langgraph-example",
        "crewai-example",
        "autogen-example",
        "adk-example",
    }
    citation = yaml.safe_load((REPO_ROOT / "CITATION.cff").read_text(encoding="utf-8"))
    npm = json.loads((REPO_ROOT / "npm" / "package.json").read_text(encoding="utf-8"))

    assert len(loop_dirs) == 68
    assert len(loop_dirs) - len(framework_loops) == 64
    assert citation["version"] == "0.3.1"
    assert citation["url"] == "https://github.com/qualixar/bounded-loops"
    assert "68 loop folders" in citation["abstract"]
    assert npm["version"] == "0.3.1"
    assert "68" in _project()["description"] and "64" in _project()["description"]
    assert "68 loop folders" in npm["description"]
    assert "64 keyless" in npm["description"]


def test_public_docs_have_no_orphan_course_section_references() -> None:
    offenders: list[str] = []
    for root in (REPO_ROOT / "loops", REPO_ROOT / "docs"):
        for path in root.rglob("*"):
            if path.suffix not in {".md", ".sh", ".yaml"} or not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            lowered = text.lower()
            if (
                "§" in text
                or "course §" in lowered
                or "from the loop engineering course" in lowered
            ):
                offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == []
