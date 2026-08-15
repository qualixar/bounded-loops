"""The project workspace resolver — one answer to "where does this project keep its runs".

These tests exist because the alternative is two implementations of that question, which is
the same second-source-of-truth defect class the last two audit rounds were full of.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bounded_loops.domain.errors import ManifestError
from bounded_loops.workspace import (
    WORKSPACE_DIRNAME,
    discover,
    ensure,
    read_config,
    storage_root_for_loop,
)


@pytest.fixture(autouse=True)
def _no_ambient_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests exercise discovery itself, so the containment fixture must not answer for it.

    `tests/conftest.py::_isolate_project_workspace` sets $BOUNDED_LOOPS_WORKSPACE for every test
    so a forgotten `--out` cannot write into this repository. That variable short-circuits
    `discover()` to origin="explicit" — which is exactly what these tests are here to measure.
    """
    monkeypatch.delenv("BOUNDED_LOOPS_WORKSPACE", raising=False)


def _git_init(path: Path) -> None:
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=path,
        check=True,
        capture_output=True,
    )


# ── discovery ────────────────────────────────────────────────────────────────


def test_an_existing_workspace_directory_is_found_by_walking_up(tmp_path: Path) -> None:
    (tmp_path / WORKSPACE_DIRNAME).mkdir()
    deep = tmp_path / "src" / "pkg" / "inner"
    deep.mkdir(parents=True)

    workspace = discover(start=deep)

    assert workspace.project_root == tmp_path.resolve()
    assert workspace.root == (tmp_path / WORKSPACE_DIRNAME).resolve()
    assert workspace.origin == "existing"


def test_the_NEAREST_workspace_wins_when_two_are_nested(tmp_path: Path) -> None:
    (tmp_path / WORKSPACE_DIRNAME).mkdir()
    inner = tmp_path / "sub"
    inner.mkdir()
    (inner / WORKSPACE_DIRNAME).mkdir()

    assert discover(start=inner).project_root == inner.resolve()


def test_with_no_workspace_the_git_toplevel_is_chosen_and_says_so(tmp_path: Path) -> None:
    _git_init(tmp_path)
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)

    workspace = discover(start=deep)

    assert workspace.project_root == tmp_path.resolve()
    assert workspace.origin == "git-toplevel"
    # Discovery is pure: it reports where the workspace WOULD go without creating it.
    assert not workspace.root.exists()


def test_with_no_workspace_and_no_git_the_start_directory_is_chosen(tmp_path: Path) -> None:
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)

    workspace = discover(start=deep)

    assert workspace.project_root == deep.resolve()
    assert workspace.origin == "cwd"


def test_a_workspace_ABOVE_the_git_toplevel_is_NOT_borrowed(tmp_path: Path) -> None:
    """A repo's receipts must not silently land in someone else's workspace.

    Without a ceiling, `discover` walking up from a repo checked out inside a directory that
    happens to hold a `.bounded-loops/` would write this project's receipts there. The git
    toplevel is the ceiling precisely so that cannot happen.
    """
    (tmp_path / WORKSPACE_DIRNAME).mkdir()
    repo = tmp_path / "checkout"
    repo.mkdir()
    _git_init(repo)

    workspace = discover(start=repo)

    assert workspace.project_root == repo.resolve()
    assert workspace.origin == "git-toplevel"


def test_an_explicit_workspace_overrides_discovery_entirely(tmp_path: Path) -> None:
    (tmp_path / WORKSPACE_DIRNAME).mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    workspace = discover(start=tmp_path, explicit=elsewhere)

    assert workspace.project_root == elsewhere.resolve()
    assert workspace.root == (elsewhere / WORKSPACE_DIRNAME).resolve()
    assert workspace.origin == "explicit"


def test_the_environment_variable_sets_the_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    elsewhere = tmp_path / "env-root"
    elsewhere.mkdir()
    monkeypatch.setenv("BOUNDED_LOOPS_WORKSPACE", str(elsewhere))

    workspace = discover(start=tmp_path)

    assert workspace.project_root == elsewhere.resolve()
    assert workspace.origin == "explicit"


def test_an_explicit_argument_beats_the_environment_variable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_root = tmp_path / "from-env"
    env_root.mkdir()
    arg_root = tmp_path / "from-arg"
    arg_root.mkdir()
    monkeypatch.setenv("BOUNDED_LOOPS_WORKSPACE", str(env_root))

    assert discover(start=tmp_path, explicit=arg_root).project_root == arg_root.resolve()


def test_a_SYMLINKED_workspace_directory_is_REFUSED(tmp_path: Path) -> None:
    """A symlinked workspace root silently relocates every receipt in the project.

    `run_store.read_run_receipt` already refuses symlinked receipt files for the same reason;
    refusing at the root is the same rule one level up.
    """
    real = tmp_path / "real-store"
    real.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    (project / WORKSPACE_DIRNAME).symlink_to(real, target_is_directory=True)

    with pytest.raises(ManifestError, match="must not be a symlink"):
        discover(start=project)


def test_a_workspace_path_that_is_a_FILE_is_refused(tmp_path: Path) -> None:
    (tmp_path / WORKSPACE_DIRNAME).write_text("not a directory", encoding="utf-8")

    with pytest.raises(ManifestError, match="not a directory"):
        discover(start=tmp_path)


# ── layout ───────────────────────────────────────────────────────────────────


def test_the_layout_is_derived_from_the_root_and_never_stored_twice(tmp_path: Path) -> None:
    workspace = discover(start=tmp_path)
    root = workspace.root

    assert workspace.config_path == root / "config.toml"
    assert workspace.graphs_dir == root / "graphs"
    assert workspace.loops_dir == root / "loops"
    assert workspace.runs_dir == root / "runs"
    assert workspace.tickets_dir == root / "tickets"
    assert workspace.index_path == root / "index.json"


def test_a_run_directory_reuses_the_ONE_run_id_validator(tmp_path: Path) -> None:
    """A second run-id validator would be a second source of truth; there must be one."""
    workspace = discover(start=tmp_path)

    assert workspace.run_dir("run-1") == workspace.runs_dir / "run-1"
    with pytest.raises(ManifestError, match="run_id must be"):
        workspace.run_dir("../escape")


def test_ensure_creates_the_whole_layout_and_reports_what_it_made(tmp_path: Path) -> None:
    workspace = discover(start=tmp_path)

    created = ensure(workspace)

    for path in (
        workspace.root,
        workspace.graphs_dir,
        workspace.loops_dir,
        workspace.runs_dir,
        workspace.tickets_dir,
        workspace.config_path,
    ):
        assert path.exists(), path
    assert workspace.config_path in created


def test_ensure_is_idempotent_and_never_overwrites_an_existing_config(tmp_path: Path) -> None:
    workspace = discover(start=tmp_path)
    ensure(workspace)
    workspace.config_path.write_text('[workspace]\nversion = 1\nmine = true\n', encoding="utf-8")

    created = ensure(workspace)

    assert created == []
    assert "mine = true" in workspace.config_path.read_text(encoding="utf-8")


def test_the_generated_config_is_valid_toml_and_declares_its_version(tmp_path: Path) -> None:
    workspace = discover(start=tmp_path)
    ensure(workspace)

    config = read_config(workspace)

    assert config["workspace"]["version"] == 1


def test_a_missing_config_reads_as_empty_rather_than_exploding(tmp_path: Path) -> None:
    workspace = discover(start=tmp_path)

    assert read_config(workspace) == {}


def test_a_MALFORMED_config_is_a_clear_refusal_not_a_traceback(tmp_path: Path) -> None:
    workspace = discover(start=tmp_path)
    ensure(workspace)
    workspace.config_path.write_text("[workspace\nversion = ", encoding="utf-8")

    with pytest.raises(ManifestError, match="config.toml"):
        read_config(workspace)


def test_no_production_code_READS_the_index(tmp_path: Path) -> None:
    """`index.json` is a rebuildable cache. Reading it as authority is the drift bug.

    This replaces a check that asserted the words "cache" and "rebuild" appeared somewhere in two
    concatenated docstrings — which would have passed unchanged on the day someone started using
    the index as authority, since it measured the prose and not the code. Named for a property it
    did not test, which the wave-1 Muse audit flagged.

    What is measured now: `index_path` has no call site outside its own definition. `runs/` is the
    truth; anything that needs a listing scans it (see `cli_workspace._counts`, which says so). A
    new reference here is not necessarily wrong, but it must be a deliberate decision rather than
    an accident, so it fails this test first.
    """
    import ast

    from bounded_loops import workspace as workspace_module

    package = Path(workspace_module.__file__).parent
    offenders: list[str] = []

    # Proven non-empty first: every assertion below is satisfied by the empty case, so a scan
    # that found nothing would pass this while inspecting nothing.
    _scanned = sorted(package.rglob("*.py"))
    assert len(_scanned) >= 50, f"only {len(_scanned)} module(s) scanned; the walk found nothing"

    for source_file in _scanned:
        tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
        for node in ast.walk(tree):
            # The property's own definition is the one legitimate occurrence.
            if isinstance(node, ast.FunctionDef) and node.name == "index_path":
                continue
            if isinstance(node, ast.Attribute) and node.attr == "index_path":
                offenders.append(f"{source_file.relative_to(package)}:{node.lineno}")

    assert not offenders, (
        "index.json is a rebuildable cache derived from runs/, and these read it as a source: "
        + ", ".join(offenders)
    )


# ── the additive guarantee ───────────────────────────────────────────────────


def test_bl_run_receipts_STAY_package_local_so_existing_runs_stay_resumable(
    tmp_path: Path,
) -> None:
    """The locked decision is additive: no existing run directory moves.

    `bl run --run-id X` writes `<loop>/.bounded-loops/runs/X` today, and `--resume` and
    `bl runs` read from there. Redirecting loop runs into the project workspace would strand
    every 0.4.0/0.5.x run. So `storage_root_for_loop` returns None — the package-local
    default — and only the graph surface (whose `--out` had no default at all) gains one.
    """
    _git_init(tmp_path)
    loop_dir = tmp_path / "loops" / "my-loop"
    loop_dir.mkdir(parents=True)
    workspace = discover(start=loop_dir)

    assert storage_root_for_loop(workspace, loop_dir) is None


def test_opting_in_redirects_a_loop_run_into_the_project_workspace(tmp_path: Path) -> None:
    """The opt-in branch must actually do something, or the seam above is decoration.

    `run_store` appends `runs/` itself, so the value handed to it is the workspace root — not
    `runs_dir`, which would produce `.bounded-loops/runs/runs/<run_id>`.
    """
    _git_init(tmp_path)
    loop_dir = tmp_path / "loops" / "my-loop"
    loop_dir.mkdir(parents=True)
    workspace = discover(start=loop_dir)

    assert storage_root_for_loop(workspace, loop_dir, opt_in=True) == workspace.root.resolve()


def test_a_workspace_INSIDE_the_loop_package_never_reaches_the_run_store(
    tmp_path: Path,
) -> None:
    """`run_store._runs_root` raises when the storage root is inside the loop package.

    A loop package that is itself the project root would hit exactly that, even with the
    caller explicitly opting in. Returning None keeps the existing package-local behaviour
    instead of turning a legal layout into a crash.
    """
    (tmp_path / WORKSPACE_DIRNAME).mkdir()
    workspace = discover(start=tmp_path)

    assert storage_root_for_loop(workspace, tmp_path, opt_in=True) is None
