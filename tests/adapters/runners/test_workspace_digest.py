"""Acceptance tests for content-addressed workspace change detection.

The bound these protect is the no-progress soft bound. It fires only on a run of laps where the
runner reported ``changed == False``, so a detector that cannot report ``False`` disables the bound
outright — which is exactly what the previous git-based detector did from lap 2 onward.

Every test here runs in-process with no git repository and no subprocess. That is deliberate: the
defect these replace survived because detection could only be exercised through a git fixture that
no test ever built.
"""

from __future__ import annotations

import pytest

from bounded_loops.adapters.runners.workspace_digest import (
    HARNESS_ARTIFACTS,
    workspace_digest,
)


def _seed(root):
    (root / "seed").mkdir()
    (root / "seed" / "records.json").write_text('[{"id": 1}]', encoding="utf-8")
    return root


def test_identical_content_digests_equal(tmp_path):
    _seed(tmp_path)
    assert workspace_digest(tmp_path) == workspace_digest(tmp_path)


def test_editing_a_file_changes_the_digest(tmp_path):
    _seed(tmp_path)
    before = workspace_digest(tmp_path)
    (tmp_path / "seed" / "records.json").write_text('[{"id": 1, "checksum": "x"}]', encoding="utf-8")
    assert workspace_digest(tmp_path) != before


def test_adding_a_file_changes_the_digest(tmp_path):
    _seed(tmp_path)
    before = workspace_digest(tmp_path)
    (tmp_path / "seed" / "extra.txt").write_text("new", encoding="utf-8")
    assert workspace_digest(tmp_path) != before


def test_removing_a_file_changes_the_digest(tmp_path):
    _seed(tmp_path)
    before = workspace_digest(tmp_path)
    (tmp_path / "seed" / "records.json").unlink()
    assert workspace_digest(tmp_path) != before


def test_renaming_a_file_changes_the_digest_even_with_identical_bytes(tmp_path):
    _seed(tmp_path)
    before = workspace_digest(tmp_path)
    (tmp_path / "seed" / "records.json").rename(tmp_path / "seed" / "renamed.json")
    assert workspace_digest(tmp_path) != before


def test_adding_an_empty_directory_changes_the_digest(tmp_path):
    """A content-only digest would miss this; the bound should not."""
    _seed(tmp_path)
    before = workspace_digest(tmp_path)
    (tmp_path / "seed" / "empty").mkdir()
    assert workspace_digest(tmp_path) != before


@pytest.mark.parametrize("artifact", sorted(HARNESS_ARTIFACTS))
def test_harness_bookkeeping_never_counts_as_agent_progress(tmp_path, artifact):
    """THE REGRESSION. Writing our own log must not read as the agent having done something.

    The previous detector reported "changed" for the rest of the run because ``agent_output.txt``
    from lap 1 left the workspace permanently dirty against a snapshot nothing ever refreshed.
    """
    _seed(tmp_path)
    before = workspace_digest(tmp_path)
    (tmp_path / artifact).write_text("harness bookkeeping", encoding="utf-8")
    assert workspace_digest(tmp_path) == before


def test_git_directory_churn_never_counts_as_agent_progress(tmp_path):
    _seed(tmp_path)
    (tmp_path / ".git").mkdir()
    before = workspace_digest(tmp_path)
    (tmp_path / ".git" / "index").write_text("object churn", encoding="utf-8")
    assert workspace_digest(tmp_path) == before


def test_a_symlink_contributes_its_target_not_the_targets_content(tmp_path):
    """Following a link would read outside the sandbox, which is the isolation boundary."""
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    _seed(tmp_path)
    link = tmp_path / "seed" / "link"
    link.symlink_to(outside)

    before = workspace_digest(tmp_path)
    outside.write_text("secret changed", encoding="utf-8")
    assert workspace_digest(tmp_path) == before, "digest must not depend on content outside"

    link.unlink()
    link.symlink_to(tmp_path.parent / "other.txt")
    assert workspace_digest(tmp_path) != before, "retargeting the link is a change"


def test_an_absent_workspace_is_reported_not_crashed(tmp_path):
    assert workspace_digest(tmp_path / "does-not-exist") == "absent"


def test_digest_is_independent_of_directory_iteration_order(tmp_path):
    """Same content reached by different creation orders must digest identically."""
    first = tmp_path / "a"
    second = tmp_path / "b"
    for root in (first, second):
        (root / "seed").mkdir(parents=True)
    for name in ("z.txt", "m.txt", "a.txt"):
        (first / "seed" / name).write_text(name, encoding="utf-8")
    for name in ("a.txt", "z.txt", "m.txt"):
        (second / "seed" / name).write_text(name, encoding="utf-8")
    assert workspace_digest(first) == workspace_digest(second)
