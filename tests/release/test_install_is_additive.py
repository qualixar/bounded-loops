"""Installing bounded-loops must never overwrite a user's existing host configuration.

People have hooks, agents, and MCP servers configured already. A plugin that replaces any of that
gets uninstalled the same day and deserves to. Every host we support merges a plugin's own
manifest with the user's settings, so the safe path is to ship plugin-local files and let the host
combine them — and to never, anywhere, write into a user-level config ourselves.

These tests pin that property against the source tree so a future "convenience installer" cannot
quietly acquire the ability to clobber someone's setup.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOSTS = ("claude-code", "codex", "antigravity")

#: User-level config files belonging to a host. Writing any of these is what "destructive install"
#: means in practice — these are the files that already hold the user's own configuration.
_USER_CONFIG_PATTERNS = (
    r"\.claude/settings\.json",
    r"\.claude/settings\.local\.json",
    r"\.codex/config\.toml",
    r"\.gemini/settings\.json",
    r"\.antigravity/",
)

#: Write-shaped calls. Reading a user's config to REPORT on it would be fine; writing is not.
_WRITE_CALLS = ("write_text(", "write_bytes(", "open(", "shutil.copy", "shutil.move", "os.replace")

_SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".bounded-loops"}


def _shipped_sources() -> list[Path]:
    """Python and shell files that could run on a user's machine."""
    found: list[Path] = []
    for path in REPO_ROOT.rglob("*"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.suffix in {".py", ".sh"} and path.is_file():
            # Tests do not run on a user's machine, and this file names the patterns on purpose.
            if "tests" in path.parts:
                continue
            found.append(path)
    return found


def test_nothing_we_ship_writes_a_users_host_configuration() -> None:
    offenders: list[str] = []
    for path in _shipped_sources():
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern in _USER_CONFIG_PATTERNS:
            if not re.search(pattern, text):
                continue
            # Named somewhere in the file — only a problem if it is also written.
            if any(call in text for call in _WRITE_CALLS):
                offenders.append(f"{path.relative_to(REPO_ROOT)} mentions {pattern} and writes files")
    assert offenders == [], (
        "installation must be additive — a host's user config is theirs:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("host", HOSTS)
def test_each_host_pack_ships_its_own_hooks_file_rather_than_editing_the_users(host: str) -> None:
    """Plugin-local wiring is what makes the install additive: the HOST does the merging."""
    hooks_path = REPO_ROOT / "plugins" / host / "hooks" / "hooks.json"
    assert hooks_path.is_file(), f"{host} has no plugin-local hooks.json"
    data = json.loads(hooks_path.read_text(encoding="utf-8"))
    root = data.get("hooks", data)
    assert root, f"{host} hooks.json is empty"
    # Every entry must be a command hook we ship, never an instruction to replace user config.
    for event, groups in root.items():
        for group in groups:
            for entry in group.get("hooks", []):
                assert entry.get("type") == "command", f"{host}/{event} has a non-command hook"
                assert "bounded_loops.hooks." in entry.get("command", ""), (
                    f"{host}/{event} wires something other than a bounded-loops hook"
                )


def test_the_agents_snippet_is_documented_as_APPEND_only() -> None:
    """A file named `.snippet` invites a copy; someone will overwrite their AGENTS.md with it.

    The guidance has to be written down where the person doing the install is looking.
    """
    readme = (REPO_ROOT / "plugins" / "README.md").read_text(encoding="utf-8")
    assert "AGENTS.md.snippet" in readme, "the snippet's use is undocumented"
    lowered = readme.lower()
    assert "append" in lowered
    assert "do not replace" in lowered or "never replace" in lowered


def test_the_stop_hooks_off_switch_is_DISCOVERABLE_not_just_implemented() -> None:
    """A switch nobody can find is not an off switch.

    The behaviour itself is covered by real end-to-end tests in
    `tests/test_graph_run_stop_hook.py` (block becomes exit 0 with the setting, stays exit 2
    without it, and a malformed config does not silently disable it). What THIS test guards is
    discoverability: a user who has just been blocked needs to learn how to stop that from the
    generated `config.toml`, from the install docs, and from the refusal message itself — not by
    reading our source.
    """
    generated_config = (REPO_ROOT / "bounded_loops" / "workspace.py").read_text(encoding="utf-8")
    install_docs = (REPO_ROOT / "plugins" / "README.md").read_text(encoding="utf-8")
    refusal_message = (
        REPO_ROOT / "bounded_loops" / "hooks" / "graph_run_stop.py"
    ).read_text(encoding="utf-8")

    for where, text in (
        ("the generated config.toml template", generated_config),
        ("the plugin install docs", install_docs),
        ("the hook's own refusal message", refusal_message),
    ):
        assert "stop_on_active_run" in text, f"the off switch is not mentioned in {where}"
