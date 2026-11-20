"""The source package never carries a `.git` pointer file.

In a git worktree, `.git` is a one-line FILE (`gitdir: /home/<user>/...`), not a
directory. The build backend skips a `.git` directory by default but not a file,
so an sdist built from a worktree used to publish the builder's absolute path.
CI checks out a real `.git` directory, so this test recreates the worktree shape
in a temporary copy of the tracked tree and builds from there.
"""

from __future__ import annotations

import subprocess
import sys
import tarfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _tracked_tree(dest: Path) -> None:
    archive = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "archive", "--format=tar", "HEAD"],
        check=True,
        capture_output=True,
    ).stdout
    dest.mkdir()
    subprocess.run(["tar", "-x", "-C", str(dest)], input=archive, check=True)
    # Carry the working copy's pyproject so an uncommitted exclude change is what is tested.
    (dest / "pyproject.toml").write_bytes((REPO_ROOT / "pyproject.toml").read_bytes())


def test_an_sdist_built_from_a_worktree_has_no_git_pointer(tmp_path: Path) -> None:
    src = tmp_path / "src"
    _tracked_tree(src)
    (src / ".git").write_text("gitdir: /home/someone/checkout/.git/worktrees/release\n")
    out = tmp_path / "dist"
    subprocess.run(
        [sys.executable, "-m", "build", "--sdist", "--outdir", str(out), str(src)],
        check=True,
        capture_output=True,
    )
    (sdist,) = out.glob("*.tar.gz")
    with tarfile.open(sdist) as archive:
        top_level = {Path(name).parts[1] for name in archive.getnames() if len(Path(name).parts) > 1}
    assert ".git" not in top_level
