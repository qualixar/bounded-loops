"""Release contracts exercised against the source tree and built metadata."""

from __future__ import annotations

import json
import re
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
    #
    # Raised again to 3600 for 0.6.9, for three additions, all of them things a reader could not
    # get from this page before:
    #
    #   1. "Why you would want this" — the problem in plain language, plus a three-row table
    #      defining gate, bound and ledger. Those three words appeared from the header onward and
    #      were never defined anywhere in the README. A non-technical reader could not learn from
    #      this page what the product was for; the terms were only ever used, never introduced.
    #   2. "Use it with Claude Code or Codex" — the actual install and invocation, above the fold.
    #      The only agent-integration content lived at line 382 of 480, opened with MCP protocol
    #      revisions and HMAC token internals, and contained NO Claude Code commands at all — it
    #      pointed at plugins/README.md instead. The single largest group of prospective users had
    #      to leave the page to find out whether the tool worked with the agent they already run.
    #   3. Captions and a second diagram in Architecture. The README shipped one diagram, for the
    #      loop engine, while half the package is the graph engine.
    #
    # Each links OUT for depth — the MCP section, plugins/README.md, docs/ARCHITECTURE.md — and
    # none of them inlines documentation, which is the condition this guard actually cares about.
    #
    # Raised to 3700 for 0.7.4's native Hermes pack install. The old two-line
    # repository-subdirectory shorthand was not a verified public contract.
    # The replacement must show the release asset, dry-run its SHA-pinned
    # contents, and install it; those are executable supply-chain steps, not
    # prose that belongs in a second document.
    assert len(readme.split()) <= 3700
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


def test_every_version_bearing_file_is_covered_by_this_contract() -> None:
    """The release has SIX version sites and this test is the list of them.

    Two were missing until 0.6.5: `plugins/antigravity/plugin.json` and the claude marketplace
    manifest. Both were bumped by hand every release and neither was checked, so either could have
    shipped stale and the only symptom would have been a plugin reporting the wrong version to a
    user. A hand-synchronised constant that nothing compares is the drift defect this project has
    now removed from three other places.

    Discovery is by scan, not by list, so a seventh site added later fails here rather than being
    silently unchecked.
    """
    import re  # noqa: PLC0415

    version_pattern = re.compile(
        r'(?:"version"\s*:\s*|^version\s*[:=]\s*|__version__\s*=\s*)"(\d+\.\d+\.\d+)"', re.M
    )
    # Scoped to files that carry the RELEASE version. A broader sweep picks up graph manifest
    # schema versions, which are a different number that must not track the package at all —
    # asserting they match would couple two unrelated things and make one of them unchangeable.
    candidates = [
        REPO_ROOT / "pyproject.toml",
        REPO_ROOT / "CITATION.cff",
        REPO_ROOT / "npm" / "package.json",
        REPO_ROOT / "bounded_loops" / "__init__.py",
        # Globbed, not listed: a new plugin manifest is then covered on the day it is added.
        *sorted(REPO_ROOT.glob("plugins/**/*.json")),
    ]

    found: dict[str, str] = {}
    for path in candidates:
        if not path.is_file() or "node_modules" in path.parts:
            continue
        match = version_pattern.search(path.read_text(encoding="utf-8"))
        if match:
            found[str(path.relative_to(REPO_ROOT))] = match.group(1)

    assert len(found) >= 6, (
        f"only {len(found)} release version sites found ({sorted(found)}); at 0.6.5 there were six. "
        "Refusing to report them consistent from a scan that did not find them."
    )
    stale = {name: value for name, value in found.items() if value != _PYPROJECT_VERSION}
    assert not stale, (
        f"version sites disagree with pyproject.toml ({_PYPROJECT_VERSION}):\n  "
        + "\n  ".join(f"{name}: {value}" for name, value in sorted(stale.items()))
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

    assert len(loop_dirs) == 69
    assert len(loop_dirs) - len(framework_loops) == 65
    assert citation["version"] == _PYPROJECT_VERSION, (
        f"CITATION.cff version ({citation['version']!r}) must match "
        f"pyproject.toml ({_PYPROJECT_VERSION!r})"
    )
    assert citation["url"] == "https://github.com/qualixar/bounded-loops"
    assert "69 loop folders" in citation["abstract"]
    assert npm["version"] == _PYPROJECT_VERSION, (
        f"npm package.json version ({npm['version']!r}) must match "
        f"pyproject.toml ({_PYPROJECT_VERSION!r})"
    )
    assert "69" in _project()["description"] and "65" in _project()["description"]
    assert "69 loop folders" in npm["description"]
    assert "65 keyless" in npm["description"]


def test_the_npm_launcher_never_installs_into_a_managed_interpreter() -> None:
    """The npm launcher must not `pip install` into the user's Python, and must never override PEP 668.

    0.6.9 shipped a launcher whose only install path was `python -m pip install bounded-loops==<v>`
    into whichever interpreter it found first. On a managed interpreter — Homebrew Python, Debian's
    python3, any distro build — PEP 668 refuses that, so `npx bounded-loops` died on a stock macOS
    machine and printed "Install it manually: pip install bounded-loops==0.6.9", which is the command
    that had just been refused. It also never looked for an engine already installed by pipx or
    `uv tool`, so a user with a working `bl` on PATH was sent down the install path regardless.

    Nothing in the suite exercised that file, which is why it shipped. This test is static on
    purpose — it needs no Node and no network, so it runs everywhere — and it guards the two things
    that actually matter: the install target, and the shortcut nobody should take.

    The tempting wrong fix is `--break-system-packages`. It exists to override exactly the protection
    PEP 668 provides, and a launcher that quietly mutates a distro-managed interpreter to save the
    user one step is not a tradeoff this package gets to make on their behalf. Asserted absent.
    """
    launcher = (REPO_ROOT / "npm" / "bin" / "bounded-loops.js").read_text(encoding="utf-8")
    # Comments stripped before the flag check: the launcher's header explains at length why
    # `--break-system-packages` is not used, and a test that cannot tell a decision from its
    # explanation would force the file to stop recording the reasoning to stay green.
    code = "\n".join(
        line for line in launcher.splitlines() if not line.lstrip().startswith("//")
    )

    assert "--break-system-packages" not in code, (
        "the launcher must never override PEP 668; create a virtual environment instead"
    )

    # The install must target the launcher's own venv interpreter, not the discovered one. Both are
    # in this file, so the assertion is about WHICH is passed to the install call.
    install = re.search(
        r"spawnSync\(\s*(?P<target>\w+)\s*,\s*\[\s*'-m',\s*'pip',\s*'install'", code
    )
    assert install is not None, "no pip install call found; if the launcher changed shape, update this"
    assert install.group("target") == "managed", (
        f"pip install targets {install.group('target')!r}; it must target the managed venv "
        "interpreter, or PEP 668 refuses it on every distro-managed Python"
    )

    # The three resolution steps that make the install a last resort rather than the only path.
    assert "'-m', 'venv'" in code, "the launcher must create its own virtual environment"
    assert "pathCliVersion" in code, (
        "the launcher must look for a matching `bl` already on PATH — that is what a pipx or "
        "`uv tool` install looks like, and those venvs are invisible to the interpreter probe"
    )
    assert "engineVersion" in code, "the launcher must verify an EXACT version before handing off"

    # A launcher pinned to a version the package does not build cannot resolve anything.
    npm = json.loads((REPO_ROOT / "npm" / "package.json").read_text(encoding="utf-8"))
    assert npm["version"] == _PYPROJECT_VERSION


def test_no_shipped_text_states_a_stale_catalog_count() -> None:
    """Every sentence that claims how many loops ship must agree with how many ship.

    The sibling test above already asserted the canonical count — in CITATION.cff's abstract, npm's
    `description`, and pyproject's `description`. Three fields. Meanwhile `bl loops --help` told users
    "all 68 shipped loops", npm/README.md said "the 68 loop folders (64 keyless)", docs/EMBEDDING.md
    said "all 68 shipped loops", and five internal comments repeated 68 — while `loops/` held 69 and
    `bl loops list` printed 69. A contract named "release metadata uses the canonical count" that
    covers three of twelve sites is the shape of defect this project exists to name, in its own gate.

    Scoped to phrases that can only mean the catalogue total, so an example saying "3 loops" is not a
    false positive. CHANGELOG.md is excluded on purpose: its old entries record what was true at the
    release they describe, and rewriting history to match today would be the actual dishonesty.
    """
    total = len(sorted((REPO_ROOT / "loops").glob("*/loop.yaml")))
    keyless = total - 4   # the four framework examples need an SDK key; see the sibling test
    patterns = {
        r"(\d+)\s+shipped\s+loops?": total,
        r"(\d+)\s+loop\s+folders": total,
        r"(\d+)\s+shipped\s+packages": total,
        # Parenthesised only: the catalogue form is "69 loop folders (65 keyless)". A bare
        # `\d+ keyless` also matched "L1 keyless demo" and "P0/P1 keyless" in loop bounds and in
        # composition's runner comment — this test's first run reported seven false positives, which
        # is the argument for running a new gate before trusting it.
        r"\((\d+)\s+keyless\)": keyless,
    }

    roots = [REPO_ROOT / "bounded_loops", REPO_ROOT / "docs", REPO_ROOT / "npm", REPO_ROOT / "loops"]
    files = [REPO_ROOT / "README.md", REPO_ROOT / "CITATION.cff", REPO_ROOT / "pyproject.toml"]
    for root in roots:
        files.extend(
            path for path in root.rglob("*")
            if path.is_file() and path.suffix in {".py", ".md", ".json", ".yaml", ".toml"}
            and "__pycache__" not in path.parts
        )

    stale: list[str] = []
    examined = 0
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        examined += 1
        for pattern, expected in patterns.items():
            for match in re.finditer(pattern, text):
                if int(match.group(1)) != expected:
                    line = text[: match.start()].count("\n") + 1
                    stale.append(
                        f"{path.relative_to(REPO_ROOT)}:{line}  says {match.group(0)!r}, "
                        f"catalogue has {expected}"
                    )

    # Floor: without it a broken glob makes this pass by reading nothing.
    assert examined >= 100, f"file discovery collapsed to {examined}; this contract proves nothing"
    assert total == 69, f"catalogue is {total} loops; update the sibling test's literals too"
    assert not stale, (
        "shipped text states a loop count the catalogue does not have:\n  " + "\n  ".join(stale)
    )


def test_public_docs_have_no_orphan_course_section_references() -> None:
    offenders: list[str] = []
    examined = 0

    for root in (REPO_ROOT / "loops", REPO_ROOT / "docs"):
        for path in root.rglob("*"):
            if path.suffix not in {".md", ".sh", ".yaml"} or not path.is_file():
                continue
            examined += 1
            text = path.read_text(encoding="utf-8")
            lowered = text.lower()
            if (
                "§" in text
                or "course §" in lowered
                or "from the loop engineering course" in lowered
            ):
                offenders.append(str(path.relative_to(REPO_ROOT)))

    # The scan has to be shown to have scanned. `offenders == []` is satisfied by an empty walk —
    # a moved directory, a suffix filter that stops matching — and would report the docs clean
    # after reading none of them.
    assert examined >= 100, (
        f"only {examined} document(s) examined under loops/ and docs/; an empty walk satisfies "
        "the assertion below without opening a file"
    )
    assert offenders == [], (
        "public docs reference a course section that is not shipped:\n  " + "\n  ".join(offenders)
    )


def test_readme_images_are_ABSOLUTE_because_pypi_cannot_resolve_relative_ones() -> None:
    """`pyproject.toml` sets `readme = "README.md"`, so this file becomes the PyPI project
    page. GitHub resolves a relative image path against the repo; PyPI does not, and renders a
    broken image instead.

    The failure is quiet in the worst way: the README looks perfect in the editor, perfect on
    GitHub, and broken on the page most people arrive at from `pip install`.
    """
    import re

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    declared = re.findall(r'!\[[^\]]*\]\(([^)]+)\)', readme)
    relative = [url for url in declared if not url.startswith(("http://", "https://"))]

    # Existence obligation (0.6.5): a README that declares no images at all satisfies "no relative
    # image is used" without checking anything, and would keep passing if every image were dropped.
    assert declared, "README declares no images; this guard would pass over nothing"

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
    referenced = [
        url for url in re.findall(r'!\[[^\]]*\]\((https://raw\.githubusercontent\.com/\S+?)\)', readme)
        if url.startswith(prefix)
    ]
    missing = [url[len(prefix):] for url in referenced
               if not (REPO_ROOT / url[len(prefix):]).exists()]

    # Existence obligation (0.6.5): with no screenshots referenced, "none are missing" is true and
    # checks nothing -- including in the case where they were all removed by accident.
    assert referenced, "README references no repository screenshots; this guard checks nothing"

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
