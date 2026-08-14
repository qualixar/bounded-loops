"""Acceptance tests for bounded_loops/hooks/pretooluse_loop_package.py.

The PreToolUse hook warns (but does NOT block) when the assistant is about to
hand-edit a file inside a directory that is a loop package (contains loop.yaml),
because such an edit changes the package content and invalidates any graph
manifest that pins the package by sha256 digest.

The hook MUST:
- Warn and allow (exit 0) when editing inside a loop package.
- Allow silently (exit 0) when editing outside any loop package.
- Never block (never exit non-zero).
- Fail open on any parse / path error.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from bounded_loops.hooks.pretooluse_loop_package import (
    main,
    _is_inside_loop_package,
    _extract_file_path,
)


# ── _is_inside_loop_package ───────────────────────────────────────────────────


def test_file_inside_package_detected(tmp_path: Path) -> None:
    """A file inside a directory with loop.yaml is inside a loop package."""
    pkg = tmp_path / "my-loop"
    pkg.mkdir()
    (pkg / "loop.yaml").write_text("gate:\n  kind: command\n  run: true\n")
    target = pkg / "seed" / "example.py"
    target.parent.mkdir()
    target.touch()
    assert _is_inside_loop_package(target) is True


def test_file_outside_package_not_detected(tmp_path: Path) -> None:
    """A file in a directory without loop.yaml is not inside a loop package."""
    target = tmp_path / "regular-file.py"
    target.touch()
    assert _is_inside_loop_package(target) is False


def test_loop_yaml_itself_is_detected(tmp_path: Path) -> None:
    """Editing loop.yaml directly must trigger the warning."""
    pkg = tmp_path / "my-loop"
    pkg.mkdir()
    (pkg / "loop.yaml").write_text("gate:\n  kind: command\n  run: true\n")
    assert _is_inside_loop_package(pkg / "loop.yaml") is True


def test_file_in_ancestor_package_is_detected(tmp_path: Path) -> None:
    """A deeply nested file whose ancestor directory has loop.yaml is inside a package."""
    pkg = tmp_path / "my-loop"
    (pkg / "seed" / "deep" / "nested").mkdir(parents=True)
    (pkg / "loop.yaml").write_text("gate:\n  kind: command\n  run: true\n")
    target = pkg / "seed" / "deep" / "nested" / "file.py"
    target.touch()
    assert _is_inside_loop_package(target) is True


def test_nonexistent_file_does_not_crash(tmp_path: Path) -> None:
    """A file path that doesn't exist yet must not crash the hook."""
    target = tmp_path / "not-yet-created.py"
    # Should return False (no loop package found walking up absent parents)
    result = _is_inside_loop_package(target)
    assert isinstance(result, bool)


def test_package_detection_stops_at_filesystem_root(tmp_path: Path) -> None:
    """Walking up from a non-package path must stop at the root without crashing."""
    target = tmp_path / "plain.py"
    assert _is_inside_loop_package(target) is False


# ── _extract_file_path ────────────────────────────────────────────────────────


def test_extract_file_path_write_tool(tmp_path: Path) -> None:
    """Write tool payload uses 'file_path' key."""
    payload = {"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / "a.py")}}
    assert _extract_file_path(payload) == str(tmp_path / "a.py")


def test_extract_file_path_edit_tool(tmp_path: Path) -> None:
    """Edit tool payload uses 'file_path' key."""
    payload = {"tool_name": "Edit", "tool_input": {"file_path": str(tmp_path / "b.py")}}
    assert _extract_file_path(payload) == str(tmp_path / "b.py")


def test_extract_file_path_missing_tool_input_returns_none() -> None:
    assert _extract_file_path({"tool_name": "Write"}) is None


def test_extract_file_path_no_file_path_key_returns_none() -> None:
    payload = {"tool_name": "Write", "tool_input": {"content": "x"}}
    assert _extract_file_path(payload) is None


def test_extract_file_path_non_edit_write_tool_returns_none() -> None:
    """Tools other than file-write tools should not trigger the hook."""
    payload = {"tool_name": "Bash", "tool_input": {"command": "ls"}}
    assert _extract_file_path(payload) is None


# ── main() ────────────────────────────────────────────────────────────────────


def test_main_edit_inside_package_warns_but_allows(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Edit inside a loop package: warn to stderr, exit 0 (allow)."""
    pkg = tmp_path / "my-loop"
    pkg.mkdir()
    (pkg / "loop.yaml").write_text("gate:\n  kind: command\n  run: true\n")
    target = str(pkg / "seed" / "example.py")
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": target},
    }
    monkeypatch.setattr(
        sys, "stdin",
        type("F", (), {"read": lambda self: json.dumps(payload)})(),
    )
    code = main(["pretooluse_loop_package.py"])
    assert code == 0  # must ALLOW
    captured = capsys.readouterr()
    assert captured.err  # must WARN


def test_main_edit_outside_package_silent_allow(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Edit outside a loop package: silent allow, no warning."""
    target = str(tmp_path / "regular.py")
    payload = {"tool_name": "Edit", "tool_input": {"file_path": target}}
    monkeypatch.setattr(
        sys, "stdin",
        type("F", (), {"read": lambda self: json.dumps(payload)})(),
    )
    code = main(["pretooluse_loop_package.py"])
    assert code == 0
    captured = capsys.readouterr()
    assert not captured.err  # silent


def test_main_malformed_stdin_allows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Malformed stdin must fail open (exit 0)."""
    monkeypatch.setattr(
        sys, "stdin",
        type("F", (), {"read": lambda self: "not json"})(),
    )
    code = main(["pretooluse_loop_package.py"])
    assert code == 0


def test_main_missing_file_path_in_payload_allows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A payload with no recognisable file path: allow silently."""
    monkeypatch.setattr(
        sys, "stdin",
        type("F", (), {"read": lambda self: json.dumps({"tool_name": "Bash"})})(),
    )
    assert main(["pretooluse_loop_package.py"]) == 0


def test_main_never_blocks_even_for_package_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This hook is a WARNING only — it must never return a non-zero blocking code."""
    pkg = tmp_path / "my-loop"
    pkg.mkdir()
    (pkg / "loop.yaml").write_text("gate:\n  kind: command\n  run: true\n")
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(pkg / "seed" / "test.py")},
    }
    monkeypatch.setattr(
        sys, "stdin",
        type("F", (), {"read": lambda self: json.dumps(payload)})(),
    )
    code = main(["pretooluse_loop_package.py"])
    assert code == 0, "PreToolUse loop-package hook must never block"
