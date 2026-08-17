"""A `pip install` must come with the loops the package advertises.

Until 0.6.1 it did not. `bl loops list` walked the filesystem for a `loops/` directory, found
nothing in a pip-only install, printed "No loops found", and told the user to "run from a
bounded-loops source checkout" — for a package whose front page advertises 69 loops. The
README had to instruct people to clone the repository to obtain the thing they had just
installed.

The catalog now ships inside the wheel. These tests cover the access layer; the packaging
itself is asserted in `tests/release/test_package_contract.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bounded_loops import catalog_access


def _catalog() -> Path:
    """The catalog this tree can offer — bundled in a wheel, or loops/ in a checkout.

    Never skips. A test that skips in the environment where it normally runs is a test that
    reports green while checking nothing, which this repo has been bitten by before.
    """
    root = catalog_access.catalog_root()
    assert root is not None, "no catalog found in either the wheel or the source tree"
    return root


def test_a_checkout_and_a_wheel_both_resolve_a_catalog() -> None:
    """`bl loops install` must work for the people building it, not only for end users."""
    assert catalog_access.catalog_root() is not None
    assert catalog_access.packaged_loop_names(), "the catalog resolved but is empty"


def test_installing_a_loop_copies_its_SOURCE(tmp_path: Path) -> None:
    root = _catalog()
    name = next(
        entry.name for entry in sorted(root.iterdir())
        if entry.is_dir() and (entry / "loop.yaml").is_file()
    )

    installed = catalog_access.install_loop(name, tmp_path)

    assert installed == tmp_path / name
    assert (installed / "loop.yaml").is_file(), "a loop without its manifest is not a loop"


def test_installing_NEVER_carries_somebody_elses_receipts(tmp_path: Path) -> None:
    """A freshly installed loop must start from the package's own state.

    Copying a `.ledger.jsonl` in would hand the user a record of a run they did not perform,
    against a workspace that no longer exists — evidence for something that did not happen
    here, which is the one thing this engine exists to prevent.
    """
    _catalog()
    names = catalog_access.packaged_loop_names()

    # Existence obligation (0.6.5): without it an empty catalog passes this guard silently,
    # which is the defect class this suite exists to find, in a guard about install isolation.
    assert len(names) >= 50, f"catalog resolved {len(names)} loops; this guard checks nothing"

    for name in names:
        installed = catalog_access.install_loop(name, tmp_path / name)
        leaked = [
            path.name for path in installed.rglob("*")
            if path.name in {".ledger.jsonl", ".bounded-loops", "__pycache__"}
        ]
        assert not leaked, f"{name} shipped run artifacts: {leaked}"


def test_an_existing_target_is_REFUSED_rather_than_merged(tmp_path: Path) -> None:
    """Half-overwriting a loop leaves a manifest that no longer matches its own seed."""
    _catalog()
    name = catalog_access.packaged_loop_names()[0]
    catalog_access.install_loop(name, tmp_path)

    with pytest.raises(FileExistsError):
        catalog_access.install_loop(name, tmp_path)

    # And --overwrite works.
    again = catalog_access.install_loop(name, tmp_path, overwrite=True)
    assert (again / "loop.yaml").is_file()


def test_overwrite_REFUSES_a_target_that_is_not_a_loop(tmp_path: Path) -> None:
    """`--overwrite` reaches an rmtree. It must only ever remove a loop package.

    If the caller pointed at the wrong directory, deleting it is the mistake — not the thing
    to do politely.
    """
    _catalog()
    name = catalog_access.packaged_loop_names()[0]
    precious = tmp_path / name
    precious.mkdir(parents=True)
    (precious / "someone-elses-work.txt").write_text("do not delete me", encoding="utf-8")

    with pytest.raises(FileExistsError):
        catalog_access.install_loop(name, tmp_path, overwrite=True)

    assert (precious / "someone-elses-work.txt").is_file()


@pytest.mark.parametrize("hostile", ["..", "../..", "a/b", "", ".", "/etc", "x/../../y"])
def test_a_hostile_name_cannot_reach_outside_the_destination(tmp_path: Path, hostile: str) -> None:
    """`name` is joined onto a path and can reach an rmtree, so it is validated first."""
    _catalog()

    with pytest.raises(LookupError):
        catalog_access.install_loop(hostile, tmp_path, overwrite=True)


def test_an_unknown_loop_is_a_LookupError_not_a_traceback(tmp_path: Path) -> None:
    _catalog()

    with pytest.raises(LookupError, match="no loop named"):
        catalog_access.install_loop("definitely-not-a-shipped-loop", tmp_path)
