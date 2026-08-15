"""Discovery must not walk out of the tree the user owns.

A repository bounds the upward search at its root: a checkout's receipts must not land in a
workspace that happens to sit above it. A **gitless** tree had no bound at all and walked to the
filesystem root, so one stray `.bounded-loops/` high up — in `$HOME`, in `/`, in a shared parent on
a multi-user host — would capture every gitless project on the machine and put their receipts
somewhere nobody chose. Found by the wave-1 Muse audit.

Home is the fallback ceiling: the outermost directory a user can be said to own. A deliberate
per-user workspace at `~/.bounded-loops` still resolves; anything above it does not.

`Path.home` is patched throughout so these assert the same facts on any machine and never read or
write the real home directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bounded_loops.workspace import discover


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home directory under `tmp_path`, with a real parent above it to reach past."""
    home = tmp_path / "home" / "someone"
    home.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("BOUNDED_LOOPS_WORKSPACE", raising=False)
    return home


def test_a_gitless_tree_does_not_borrow_a_workspace_from_ABOVE_home(fake_home: Path) -> None:
    """The defect itself: without a ceiling this resolved to the workspace outside home."""
    outside = fake_home.parent.parent  # tmp_path, above the fake home
    (outside / ".bounded-loops").mkdir()

    project = fake_home / "projects" / "gitless"
    project.mkdir(parents=True)

    workspace = discover(project)

    assert workspace.root != outside / ".bounded-loops", (
        "discovery walked past home and adopted a workspace the user never chose for this project"
    )
    assert workspace.origin == "cwd"
    assert workspace.project_root == project


def test_a_deliberate_workspace_AT_home_is_still_found(fake_home: Path) -> None:
    """The ceiling is inclusive. Stopping before home would break a real per-user setup.

    Without this the test above could pass by refusing everything above the starting directory,
    which would be a different bug rather than a fix.
    """
    (fake_home / ".bounded-loops").mkdir()

    project = fake_home / "projects" / "gitless"
    project.mkdir(parents=True)

    workspace = discover(project)

    assert workspace.project_root == fake_home
    assert workspace.origin == "existing"


def test_a_workspace_between_the_project_and_home_still_wins(fake_home: Path) -> None:
    """Nearest-first is unchanged; the ceiling only stops the walk, it does not reorder it."""
    (fake_home / ".bounded-loops").mkdir()
    middle = fake_home / "projects"
    middle.mkdir(parents=True)
    (middle / ".bounded-loops").mkdir()

    project = middle / "gitless"
    project.mkdir()

    workspace = discover(project)

    assert workspace.project_root == middle, "the nearer workspace must still take precedence"


def test_a_git_root_still_bounds_the_walk_even_inside_home(fake_home: Path) -> None:
    """The repository rule is unchanged and remains tighter than the home rule when both apply."""
    (fake_home / ".bounded-loops").mkdir()
    repo = fake_home / "projects" / "repo"
    repo.mkdir(parents=True)
    (repo / ".git").mkdir()

    project = repo / "src" / "deep"
    project.mkdir(parents=True)

    workspace = discover(project)

    assert workspace.project_root == repo, (
        "a checkout must not adopt the home workspace — the git root is the tighter bound"
    )
    assert workspace.origin == "git-toplevel"


def test_a_path_outside_home_keeps_the_old_unbounded_behaviour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ceiling is inferred for a tree the user does not own — a system path, another user's
    directory, a container with no usable HOME.

    Narrowing discovery there would silently break callers who legitimately keep a workspace above
    the starting directory, and this fix is about not reaching past what the user owns, not about
    reaching less in general.
    """
    home = tmp_path / "home" / "someone"
    home.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("BOUNDED_LOOPS_WORKSPACE", raising=False)

    elsewhere = tmp_path / "srv" / "shared"
    elsewhere.mkdir(parents=True)
    (elsewhere / ".bounded-loops").mkdir()
    project = elsewhere / "gitless"
    project.mkdir()

    workspace = discover(project)

    assert workspace.project_root == elsewhere


def test_an_unresolvable_home_does_not_break_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`Path.home()` raises when there is no HOME and no passwd entry. Discovery must survive it —
    resolving a workspace is on the path of every CLI invocation."""
    def _boom(cls):  # noqa: ANN001
        raise RuntimeError("no home directory")

    monkeypatch.setattr(Path, "home", classmethod(_boom))
    monkeypatch.delenv("BOUNDED_LOOPS_WORKSPACE", raising=False)

    project = tmp_path / "gitless"
    project.mkdir()

    workspace = discover(project)

    assert workspace.project_root == project
