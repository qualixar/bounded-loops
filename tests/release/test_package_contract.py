"""Release contracts exercised against the source tree and built metadata."""

from __future__ import annotations

import json
import struct
import tomllib
from pathlib import Path

import pytest
import yaml

from bounded_loops import __version__ as _PACKAGE_VERSION


REPO_ROOT = Path(__file__).resolve().parents[2]

# Single canonical source for the expected version used in cross-surface checks.
# Reading from pyproject at module load time means the next bump never leaves a
# stale literal here — only pyproject.toml (and the surfaces that must agree with
# it) need updating.  All cross-surface assertions below use this constant so that
# a disagreement still causes a failure, just not from a hardcoded literal.
_PYPROJECT_VERSION = tomllib.loads(
    (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
)["project"]["version"]


def _project() -> dict:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]


def test_default_install_includes_pytest_for_shipped_pytest_gates() -> None:
    dependencies = _project()["dependencies"]
    assert any(dependency.lower().startswith("pytest>=") for dependency in dependencies)


def test_pyproject_version_matches_package_runtime_version() -> None:
    """pyproject.toml version and bounded_loops.__version__ must agree.

    This is a cross-surface check: a bump that updates pyproject but forgets
    bounded_loops/__init__.py (or vice versa) will fail here.  It replaces the
    old literal-comparison test that would silently rot on the next release.
    """
    assert _PYPROJECT_VERSION == _PACKAGE_VERSION, (
        f"pyproject.toml version ({_PYPROJECT_VERSION!r}) and "
        f"bounded_loops.__version__ ({_PACKAGE_VERSION!r}) are out of sync — "
        "update both together on every release"
    )


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
    # Raised from 2200 in the P4.5 audit round. Three findings (F6 aliasing qualifier on the
    # capability matrix, the corrected `kind: loop` isolation claim, F13's catalog-is-not-in-the-wheel
    # note) each required words that make a claim narrower rather than louder, and the honest
    # capability matrix trades brevity for accuracy on purpose. The guard exists to stop a README
    # ballooning into documentation; cutting a qualifier to hit a round number is the wrong
    # direction for a project whose selling point is not overclaiming.
    #
    # Raised again to 3000 for 0.6.0, for three additions rather than a general loosening:
    #
    #   1. A screenshots section. This release ships a UI, and a README that describes one
    #      without showing it is asking the reader to take it on faith.
    #   2. `bl monitor` itself, which had no mention anywhere despite being the headline
    #      feature. The depth lives in docs/monitor.md; the README carries the summary.
    #   3. The confirm-token handshake. That contract CHANGED in 0.6.0 — a caller passing
    #      confirm=true without a token is now refused — so omitting it strands every reader
    #      who integrated against 0.5.
    #
    # The intent of the guard is "do not turn the README into the documentation", and each of
    # these links OUT to docs rather than inlining them. If the next increase cannot name its
    # additions this specifically, trim instead of raising.
    assert len(readme.split()) <= 3000
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


def test_readme_architecture_diagram_is_a_large_regenerable_png() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    preview = REPO_ROOT / "docs" / "diagrams" / "ports-and-adapters.png"
    renderer = REPO_ROOT / "scripts" / "render_architecture_diagram.py"

    assert "docs/diagrams/ports-and-adapters.png" in readme
    assert preview.is_file()
    assert renderer.is_file()
    payload = preview.read_bytes()
    assert payload.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = struct.unpack(">II", payload[16:24])
    assert (width, height) == (1800, 1600)


def test_codex_plugin_uses_current_manifest_contract() -> None:
    plugin_root = REPO_ROOT / "plugins" / "codex"
    manifest = json.loads(
        (plugin_root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    assert manifest["name"] == "bounded-loops"
    assert manifest["version"] == _PYPROJECT_VERSION, (
        f"codex plugin version ({manifest['version']!r}) must match "
        f"pyproject.toml ({_PYPROJECT_VERSION!r})"
    )
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
    assert manifest["version"] == _PYPROJECT_VERSION, (
        f"claude plugin version ({manifest['version']!r}) must match "
        f"pyproject.toml ({_PYPROJECT_VERSION!r})"
    )


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
    assert citation["version"] == _PYPROJECT_VERSION, (
        f"CITATION.cff version ({citation['version']!r}) must match "
        f"pyproject.toml ({_PYPROJECT_VERSION!r})"
    )
    assert citation["url"] == "https://github.com/qualixar/bounded-loops"
    assert "68 loop folders" in citation["abstract"]
    assert npm["version"] == _PYPROJECT_VERSION, (
        f"npm package.json version ({npm['version']!r}) must match "
        f"pyproject.toml ({_PYPROJECT_VERSION!r})"
    )
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


def test_readme_images_are_ABSOLUTE_because_pypi_cannot_resolve_relative_ones() -> None:
    """`pyproject.toml` sets `readme = "README.md"`, so this file becomes the PyPI project
    page. GitHub resolves a relative image path against the repo; PyPI does not, and renders a
    broken image instead.

    The failure is quiet in the worst way: the README looks perfect in the editor, perfect on
    GitHub, and broken on the page most people arrive at from `pip install`.
    """
    import re

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    relative = [
        m.group(1)
        for m in re.finditer(r'!\[[^\]]*\]\(([^)]+)\)', readme)
        if not m.group(1).startswith(("http://", "https://"))
    ]

    assert not relative, (
        "these README images use relative paths and will render broken on PyPI:\n  "
        + "\n  ".join(relative)
        + "\nUse https://raw.githubusercontent.com/qualixar/bounded-loops/main/<path>."
    )


def test_every_README_screenshot_actually_EXISTS_in_the_repo() -> None:
    """An absolute raw.githubusercontent URL renders broken just as easily if the file was
    never committed — and unlike a relative path, nothing local catches it."""
    import re

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    prefix = "https://raw.githubusercontent.com/qualixar/bounded-loops/main/"
    missing = [
        url[len(prefix):]
        for url in re.findall(r'!\[[^\]]*\]\((https://raw\.githubusercontent\.com/\S+?)\)', readme)
        if url.startswith(prefix) and not (REPO_ROOT / url[len(prefix):]).exists()
    ]

    assert not missing, f"README references images that are not in the repo: {missing}"


def test_the_BUILT_WHEEL_carries_the_loop_catalog() -> None:
    """`pip install bounded-loops` must come with the loops the front page advertises.

    Until 0.6.1 it did not. The catalog lived only in the git repository, so a pip-only user
    ran `bl loops list`, saw "No loops found", and was told to "run from a bounded-loops source
    checkout" — by a package whose README opens by advertising 68 of them.

    Inspects the built artifact rather than the source tree on purpose: the source tree has
    `loops/` either way, so only the wheel can answer this. Skips when nothing is built,
    because `uv build` is not something a unit test should trigger.
    """
    import zipfile

    wheels = sorted((REPO_ROOT / "dist").glob("bounded_loops-*-py3-none-any.whl"))
    if not wheels:
        pytest.skip("no built wheel in dist/ — run `uv build` first")
    newest = max(wheels, key=lambda p: p.stat().st_mtime)

    with zipfile.ZipFile(newest) as archive:
        catalog = [n for n in archive.namelist() if n.startswith("bounded_loops/catalog/loops/")]

    loops = {n.split("/")[3] for n in catalog if len(n.split("/")) > 4}
    assert len(loops) >= 60, (
        f"{newest.name} carries {len(loops)} loops; the catalog is not in the wheel"
    )
    assert any(n.endswith("/loop.yaml") for n in catalog), "no manifests in the packaged catalog"


def test_the_packaged_catalog_carries_no_RUN_ARTIFACTS() -> None:
    """A shipped loop carries its source, never somebody else's receipts.

    `force-include` bypasses hatchling's exclude rules, so a build from a dirty working tree
    will happily bake the developer's own `.ledger.jsonl` and `.bounded-loops/runs/` into the
    wheel. Those files are gitignored, so a clean checkout builds clean — but "we usually build
    clean" is not a guarantee, and shipping a receipt for a run the user never performed is the
    exact claim this engine exists to prevent.
    """
    import zipfile

    wheels = sorted((REPO_ROOT / "dist").glob("bounded_loops-*-py3-none-any.whl"))
    if not wheels:
        pytest.skip("no built wheel in dist/ — run `uv build` first")
    newest = max(wheels, key=lambda p: p.stat().st_mtime)

    with zipfile.ZipFile(newest) as archive:
        polluted = [
            name for name in archive.namelist()
            if name.startswith("bounded_loops/catalog/")
            and (
                name.endswith(".ledger.jsonl")
                or "/.bounded-loops/" in name
                or "/__pycache__/" in name
            )
        ]

    assert not polluted, (
        f"{newest.name} ships run artifacts — rebuild from a clean tree:\n  "
        + "\n  ".join(polluted[:8])
    )
