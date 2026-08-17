"""SEC-04: what `_copy_back` promotes, and where it is allowed to land.

The finding named `st_nlink`. Running the three cases showed the finding's mechanism
is not an escalation and that two neighbouring cases are: a symlink already at the
destination let promotion overwrite a file outside the workspace, and a symlinked
destination directory let it create one. Both are asserted here; the hardlink case is
asserted too, as the documented decision that it is allowed and why.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from bounded_loops.adapters.runners.worktree import _copy_back
from bounded_loops.domain.errors import RunnerError


def test_a_symlink_at_the_destination_cannot_be_written_through(tmp_path: Path) -> None:
    """Before the fix this replaced the outside file's contents with the agent's."""
    outside = tmp_path / "outside.txt"
    outside.write_text("ORIGINAL", encoding="utf-8")
    src = tmp_path / "src"
    src.mkdir()
    (src / "x.txt").write_text("FROM-AGENT", encoding="utf-8")
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "x.txt").symlink_to(outside)

    with pytest.raises(RunnerError, match="symlink already at the destination"):
        _copy_back(src, dest)
    assert outside.read_text(encoding="utf-8") == "ORIGINAL", "the outside file is untouched"


def test_a_symlinked_destination_directory_cannot_receive_a_promotion(tmp_path: Path) -> None:
    """`mkdir(exist_ok=True)` succeeds against a link, so the containment check is separate."""
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "sub" / "y.txt").write_text("ESCAPED", encoding="utf-8")
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "sub").symlink_to(outside_dir, target_is_directory=True)

    with pytest.raises(RunnerError, match="symlink already at the destination"):
        _copy_back(src, dest)
    assert not (outside_dir / "y.txt").exists()


def test_a_deeper_symlinked_parent_is_also_refused(tmp_path: Path) -> None:
    """The link need not be the last component for the write to land outside."""
    outside_dir = tmp_path / "outside_dir"
    (outside_dir / "deep").mkdir(parents=True)
    src = tmp_path / "src"
    (src / "a" / "b").mkdir(parents=True)
    (src / "a" / "b" / "z.txt").write_text("ESCAPED", encoding="utf-8")
    dest = tmp_path / "dest"
    (dest / "a").mkdir(parents=True)
    (dest / "a" / "b").symlink_to(outside_dir / "deep", target_is_directory=True)

    with pytest.raises(RunnerError):
        _copy_back(src, dest)
    assert not (outside_dir / "deep" / "z.txt").exists()


def test_a_source_hardlink_is_promoted_as_an_independent_copy(tmp_path: Path) -> None:
    """The audit's own case, asserted as the decision it is rather than left implicit.

    Promotion is allowed and breaks the link: the result shares no inode with the
    original, so a later write to either cannot change the other. Refusing it would
    break package stores that hardlink (pnpm) and would prevent no read the agent did
    not already have.
    """
    outside = tmp_path / "outside.txt"
    outside.write_text("SHARED", encoding="utf-8")
    src = tmp_path / "src"
    src.mkdir()
    os.link(outside, src / "innocent.txt")
    dest = tmp_path / "dest"
    dest.mkdir()

    _copy_back(src, dest)

    promoted = dest / "innocent.txt"
    assert promoted.read_text(encoding="utf-8") == "SHARED"
    assert promoted.stat().st_nlink == 1, "the promotion broke the link"
    assert promoted.stat().st_ino != outside.stat().st_ino

    promoted.write_text("CHANGED", encoding="utf-8")
    assert outside.read_text(encoding="utf-8") == "SHARED", "writes do not travel back"


def test_an_ordinary_promotion_still_works(tmp_path: Path) -> None:
    """The containment check must not refuse the normal case it is wrapped around."""
    src = tmp_path / "src"
    (src / "pkg").mkdir(parents=True)
    (src / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (src / "top.txt").write_text("hello\n", encoding="utf-8")
    dest = tmp_path / "dest"
    dest.mkdir()

    _copy_back(src, dest)

    assert (dest / "pkg" / "mod.py").read_text(encoding="utf-8") == "x = 1\n"
    assert (dest / "top.txt").read_text(encoding="utf-8") == "hello\n"
