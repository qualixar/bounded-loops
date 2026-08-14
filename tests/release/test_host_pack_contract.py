"""Release contracts for the U2 host pack.

Tests drive the implementation via TDD. Every assertion here is traceable to
a specific requirement in the U2 spec.

All claims are verifiable:
- Command existence: checked against the file system.
- SKILL.md identity: byte-for-byte comparison, not content inspection.
- Refusal doc completeness: derived from REFUSAL_CODES in refusals.py,
  not re-parsed from source, so the authoritative table and the doc stay in sync.
  (refusals.py itself is tested against validate_graph.py source in
  tests/graph/application/test_refusals.py by the orchestrator stream.)
- Agent definitions: byte-for-byte comparison across hosts.
"""

from __future__ import annotations

import json
from pathlib import Path

from bounded_loops.graph.application.refusals import REFUSAL_CODES

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SHARED_DIR = REPO_ROOT / "plugins" / "shared"
HOSTS = ("claude-code", "codex", "antigravity")

# Commands that must exist for every host that supports them.
# bl-run already exists; all others are new in U2.
REQUIRED_COMMANDS = [
    "bl-graph",
    "bl-configure",
    "bl-status",
    "bl-approve",
    "bl-metrics",
    "bl-loop",
    "bl-run",
]

# Agent definitions that must exist for every host.
AGENT_NAMES = [
    "bounded-loops-composer",
    "bounded-loops-gatekeeper",
]


# ── commands ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("host", HOSTS)
@pytest.mark.parametrize("command", REQUIRED_COMMANDS)
def test_command_exists_for_host(host: str, command: str) -> None:
    """Each command markdown file must exist in every host's commands/ directory."""
    path = REPO_ROOT / "plugins" / host / "commands" / f"{command}.md"
    assert path.is_file(), f"Missing command file: {path.relative_to(REPO_ROOT)}"


@pytest.mark.parametrize("command", REQUIRED_COMMANDS)
def test_command_content_identical_across_hosts(command: str) -> None:
    """All per-host command copies must be byte-for-byte identical to the
    canonical source in plugins/shared/commands/."""
    canonical = SHARED_DIR / "commands" / f"{command}.md"
    assert canonical.is_file(), (
        f"Canonical source missing: {canonical.relative_to(REPO_ROOT)}"
    )
    canonical_bytes = canonical.read_bytes()
    for host in HOSTS:
        host_path = REPO_ROOT / "plugins" / host / "commands" / f"{command}.md"
        host_bytes = host_path.read_bytes()
        assert host_bytes == canonical_bytes, (
            f"{host}/commands/{command}.md differs from shared canonical; "
            "run `scripts/sync_host_pack.py` or copy manually"
        )


# ── SKILL.md identity ─────────────────────────────────────────────────────────


def test_canonical_skill_exists() -> None:
    canonical = SHARED_DIR / "skills" / "bounded-loops" / "SKILL.md"
    assert canonical.is_file(), (
        f"Canonical SKILL.md missing: {canonical.relative_to(REPO_ROOT)}"
    )


@pytest.mark.parametrize("host", HOSTS)
def test_skill_md_identical_to_canonical(host: str) -> None:
    """Per-host SKILL.md copies must be byte-for-byte identical to the canonical.

    Drift between copies is the failure mode this test prevents: a host model
    sees a different capability claim than another, leading to different authoring
    behaviour from the same user request.
    """
    canonical = SHARED_DIR / "skills" / "bounded-loops" / "SKILL.md"
    assert canonical.is_file(), "Canonical SKILL.md missing"
    host_path = REPO_ROOT / "plugins" / host / "skills" / "bounded-loops" / "SKILL.md"
    assert host_path.is_file(), (
        f"SKILL.md missing for host {host!r}: {host_path.relative_to(REPO_ROOT)}"
    )
    assert host_path.read_bytes() == canonical.read_bytes(), (
        f"plugins/{host}/skills/bounded-loops/SKILL.md differs from canonical; "
        "update the canonical and re-copy"
    )


# ── agent definitions ─────────────────────────────────────────────────────────


def test_canonical_agents_exist() -> None:
    for name in AGENT_NAMES:
        path = SHARED_DIR / "agents" / f"{name}.md"
        assert path.is_file(), (
            f"Canonical agent definition missing: {path.relative_to(REPO_ROOT)}"
        )


@pytest.mark.parametrize("host", HOSTS)
@pytest.mark.parametrize("agent_name", AGENT_NAMES)
def test_agent_definition_exists_for_host(host: str, agent_name: str) -> None:
    path = REPO_ROOT / "plugins" / host / "agents" / f"{agent_name}.md"
    assert path.is_file(), (
        f"Agent definition missing for {host}: {path.relative_to(REPO_ROOT)}"
    )


@pytest.mark.parametrize("agent_name", AGENT_NAMES)
def test_agent_definition_identical_across_hosts(agent_name: str) -> None:
    canonical = SHARED_DIR / "agents" / f"{agent_name}.md"
    assert canonical.is_file(), f"Canonical agent {agent_name!r} missing"
    canonical_bytes = canonical.read_bytes()
    for host in HOSTS:
        host_path = REPO_ROOT / "plugins" / host / "agents" / f"{agent_name}.md"
        assert host_path.read_bytes() == canonical_bytes, (
            f"plugins/{host}/agents/{agent_name}.md differs from canonical"
        )


# ── refusal reference doc ─────────────────────────────────────────────────────


def test_refusal_reference_doc_exists() -> None:
    doc = SHARED_DIR / "docs" / "refusal-reference.md"
    assert doc.is_file(), (
        f"Refusal reference doc missing: {doc.relative_to(REPO_ROOT)}"
    )


def test_refusal_reference_covers_all_validator_codes() -> None:
    """Every refusal code in REFUSAL_CODES must appear in the reference doc.

    Uses REFUSAL_CODES (the authoritative table in refusals.py) rather than
    re-parsing validate_graph.py — refusals.py is itself tested against the
    source by tests/graph/application/test_refusals.py.

    This test fails if a code is added to REFUSAL_CODES but not documented.
    """
    doc = SHARED_DIR / "docs" / "refusal-reference.md"
    doc_text = doc.read_text(encoding="utf-8")
    missing = {code for code in REFUSAL_CODES if code not in doc_text}
    assert not missing, (
        f"Refusal reference doc is missing these codes: {sorted(missing)}. "
        "Add a row for each to plugins/shared/docs/refusal-reference.md"
    )


# ── hook wiring ───────────────────────────────────────────────────────────────


def test_graph_run_stop_hook_module_exists() -> None:
    """The graph-run Stop hook must exist as a Python module."""
    hook = REPO_ROOT / "bounded_loops" / "hooks" / "graph_run_stop.py"
    assert hook.is_file(), f"Missing hook: {hook.relative_to(REPO_ROOT)}"


def test_pretooluse_loop_package_hook_module_exists() -> None:
    """The PreToolUse loop-package warning hook must exist as a Python module."""
    hook = REPO_ROOT / "bounded_loops" / "hooks" / "pretooluse_loop_package.py"
    assert hook.is_file(), f"Missing hook: {hook.relative_to(REPO_ROOT)}"


@pytest.mark.parametrize("host", HOSTS)
def test_graph_run_stop_hook_wired_in_hooks_json(host: str) -> None:
    """The graph_run_stop hook must be present in every host's hooks.json.

    graph_run_stop is the killer feature of the U2 pack: it prevents the host
    from declaring done while a bounded run is still active.
    """
    hooks_path = REPO_ROOT / "plugins" / host / "hooks" / "hooks.json"
    assert hooks_path.is_file()
    text = hooks_path.read_text(encoding="utf-8")
    assert "graph_run_stop" in text, (
        f"plugins/{host}/hooks/hooks.json does not wire graph_run_stop"
    )


@pytest.mark.parametrize("host", HOSTS)
def test_pretooluse_hook_wired_in_hooks_json(host: str) -> None:
    """The pretooluse_loop_package hook must be wired for every host."""
    hooks_path = REPO_ROOT / "plugins" / host / "hooks" / "hooks.json"
    text = hooks_path.read_text(encoding="utf-8")
    assert "pretooluse_loop_package" in text, (
        f"plugins/{host}/hooks/hooks.json does not wire pretooluse_loop_package"
    )


# ── added during orchestrator review ─────────────────────────────────────────


def _pretooluse_groups(host: str) -> list[dict]:
    """The PreToolUse groups for a host, tolerating the two wiring schemas in use.

    claude-code and codex nest everything under a top-level "hooks" key; antigravity puts the
    event names at the top level. That difference is exactly where a per-host wiring change goes
    unnoticed, so this helper reads both rather than assuming one.
    """
    data = json.loads(
        (REPO_ROOT / "plugins" / host / "hooks" / "hooks.json").read_text(encoding="utf-8")
    )
    root = data.get("hooks", data)
    groups = root.get("PreToolUse", [])
    assert isinstance(groups, list) and groups, f"{host} has no PreToolUse wiring"
    return groups


@pytest.mark.parametrize("host", ["claude-code", "codex", "antigravity"])
def test_the_pretooluse_hook_is_scoped_to_file_writing_tools(host: str) -> None:
    """Without a matcher the host spawns a Python interpreter on EVERY tool call.

    The hook filters internally against `_FILE_WRITE_TOOLS` and exits 0, so the behaviour is
    correct either way — but paying an interpreter start per tool use, all session long, to
    discover there is nothing to do is a cost the matcher removes for free. The matcher must also
    stay in step with the hook's own filter, or the two disagree about which tools are checked.
    """
    from bounded_loops.hooks.pretooluse_loop_package import _FILE_WRITE_TOOLS

    for group in _pretooluse_groups(host):
        commands = [entry.get("command", "") for entry in group.get("hooks", [])]
        if not any("pretooluse_loop_package" in command for command in commands):
            continue
        matcher = group.get("matcher", "")
        assert matcher, f"{host} wires the PreToolUse hook with no tool matcher"
        assert set(matcher.split("|")) == set(_FILE_WRITE_TOOLS), (
            f"{host} matcher {matcher!r} disagrees with the hook's own "
            f"_FILE_WRITE_TOOLS {sorted(_FILE_WRITE_TOOLS)}"
        )


@pytest.mark.parametrize("host", ["claude-code", "codex", "antigravity"])
def test_the_stop_hook_is_wired_for_every_host(host: str) -> None:
    """The Stop hook is the product's thesis pointed at the orchestrator; unwired it is nothing."""
    data = json.loads(
        (REPO_ROOT / "plugins" / host / "hooks" / "hooks.json").read_text(encoding="utf-8")
    )
    root = data.get("hooks", data)
    commands = [
        entry.get("command", "")
        for group in root.get("Stop", [])
        for entry in group.get("hooks", [])
    ]
    assert any("graph_run_stop" in command for command in commands), (
        f"{host} does not wire bounded_loops.hooks.graph_run_stop on Stop"
    )


def _real_cli_surface() -> tuple[set[str], set[str]]:
    """Every `bl` command and every `bl graph` action the shipped parser actually resolves."""
    from bounded_loops.cli import _build_parser

    parser = _build_parser()
    top_level: set[str] = set()
    graph_actions: set[str] = set()
    for action in parser._actions:
        choices = getattr(action, "choices", None)
        if not isinstance(choices, dict):
            continue
        top_level |= set(choices)
        graph = choices.get("graph")
        if graph is None:
            continue
        for sub in graph._actions:
            sub_choices = getattr(sub, "choices", None)
            if isinstance(sub_choices, dict):
                graph_actions |= set(sub_choices)
    return top_level, graph_actions


def _pack_documents() -> list[tuple[str, str]]:
    """Every file in the host pack that can instruct a model to run something.

    The skill is not the only one, which is how this gap survived: the checked-only-SKILL.md
    version of this test passed while `bounded-loops-composer.md` told models to run
    `bl graph digest`, a command that did not exist at the time — offered, of all things, as the
    way to obtain the one field the same file forbids inventing. Commands and agents are prompts
    too, and a prompt naming a missing command is a model sent in a circle.
    """
    documents: list[tuple[str, str]] = [
        (
            "skills/bounded-loops/SKILL.md",
            (SHARED_DIR / "skills" / "bounded-loops" / "SKILL.md").read_text(encoding="utf-8"),
        ),
    ]
    for folder in ("commands", "agents"):
        for path in sorted((SHARED_DIR / folder).glob("*.md")):
            documents.append((f"{folder}/{path.name}", path.read_text(encoding="utf-8")))
    return documents


@pytest.mark.parametrize("relative,text", _pack_documents(), ids=lambda v: v if isinstance(v, str) and "/" in v else "")
def test_every_command_the_HOST_PACK_names_actually_exists(relative: str, text: str) -> None:
    """No shipped prompt may name a command, action, or module we do not ship."""
    import importlib.util
    import re

    for module in set(re.findall(r"python3? -m (bounded_loops[\w.]*)", text)):
        assert importlib.util.find_spec(module) is not None, (
            f"{relative} names missing module {module}"
        )

    top_level, graph_actions = _real_cli_surface()

    for command, sub in set(re.findall(r"`bl ([a-z-]+)(?: ([a-z-]+))?", text)):
        assert command in top_level, f"{relative} names `bl {command}`, which does not exist"
        if command == "graph" and sub:
            assert sub in graph_actions, (
                f"{relative} names `bl graph {sub}`, which does not exist"
            )


def test_every_MCP_TOOL_the_host_pack_names_is_registered() -> None:
    """The other half of the same failure, for the surface the pack actually drives.

    A prompt naming `graph_interview` when nothing registers it fails the same way a missing CLI
    action does, but more quietly: the model calls a tool that is not there and improvises.
    """
    import asyncio
    import re

    from bounded_loops import mcp_server

    registered = {tool.name for tool in asyncio.run(mcp_server.mcp.list_tools())}

    # A tool reference is a name followed by "(" — that is how the pack writes calls, e.g.
    # `graph_status(run=...)`. Requiring the paren is what separates a call from a schema FIELD
    # of a similar shape: `graph_package` is the sha256 field on a `subgraph` node, and an
    # earlier version of this pattern reported it as a missing tool. A guard that flags the
    # schema is a guard people learn to ignore.
    pattern = re.compile(r"`?\b(bl_[a-z_]+|graph_[a-z_]+)\(")
    missing: list[str] = []
    for relative, text in _pack_documents():
        for name in pattern.findall(text):
            if name not in registered:
                missing.append(f"{relative}: {name}")

    assert not missing, (
        "the host pack names MCP tools that are not registered:\n  " + "\n  ".join(sorted(set(missing)))
    )
