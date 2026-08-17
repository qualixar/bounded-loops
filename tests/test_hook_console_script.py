"""Task #67: hooks must not depend on which `python3` is first on the user's PATH.

The plugin manifests invoked `python3 -m bounded_loops.hooks.X`. Under pipx, uv tool, a
project venv or Homebrew Python, that interpreter frequently cannot import the package
that registered the hook, and the result is a ModuleNotFoundError inside an editor hook
where nobody reads stderr. The same mistake cost this project a whole experiment arm.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bounded_loops.hooks.cli import _HOOKS, main

_PLUGINS = Path(__file__).resolve().parents[1] / "plugins"


def _commands(node: object) -> list[str]:
    """Every `command` string anywhere in a manifest.

    Walks the whole document rather than assuming a nesting depth. The three hosts
    already differ in shape — codex and claude-code nest under a top-level `hooks` key,
    antigravity does not — and a traversal pinned to one shape finds nothing in the other
    while looking like a passing test.
    """
    if isinstance(node, dict):
        found = [node["command"]] if isinstance(node.get("command"), str) else []
        return found + [c for value in node.values() for c in _commands(value)]
    if isinstance(node, list):
        return [c for item in node for c in _commands(item)]
    return []


def test_every_hook_name_resolves_to_a_real_main() -> None:
    for name in _HOOKS:
        from importlib import import_module

        module = import_module(_HOOKS[name])
        assert callable(module.main), f"{name} does not expose a callable main"


@pytest.mark.parametrize("host", ["claude-code", "codex", "antigravity"])
def test_no_plugin_manifest_invokes_a_bare_interpreter(host: str) -> None:
    manifest = _PLUGINS / host / "hooks" / "hooks.json"
    text = manifest.read_text(encoding="utf-8")
    assert "python3 -m" not in text, (
        f"{host}: a hook resolves `python3` against the user's PATH, which need not be "
        "the interpreter this package is installed in"
    )
    assert "bounded-loops-hook " in text


@pytest.mark.parametrize("host", ["claude-code", "codex", "antigravity"])
def test_every_manifest_command_names_a_hook_the_dispatcher_knows(host: str) -> None:
    """A manifest naming a hook the dispatcher does not have is a silent no-op."""
    document = json.loads((_PLUGINS / host / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    commands = _commands(document)
    assert commands, f"{host}: no hook commands found — the shape of the manifest changed"
    for command in commands:
        parts = command.split()
        assert parts[0] == "bounded-loops-hook"
        assert parts[1] in _HOOKS, f"{host}: unknown hook {parts[1]!r} in {command!r}"


def test_the_console_script_is_declared_in_pyproject() -> None:
    """The manifests are worthless if the entry point is not installed."""
    text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    assert 'bounded-loops-hook = "bounded_loops.hooks.cli:main"' in text


def test_an_unknown_hook_does_not_block_the_editor(capsys: pytest.CaptureFixture[str]) -> None:
    """A hook that exits non-zero can wedge the action it is attached to."""
    assert main(["bounded-loops-hook", "not-a-hook", "claude-code"]) == 0
    assert "unknown hook" in capsys.readouterr().err


def test_no_arguments_prints_usage_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["bounded-loops-hook"]) == 0
    assert "usage: bounded-loops-hook" in capsys.readouterr().err
