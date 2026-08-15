"""Where the shipped loop catalog lives, and how to get a writable copy of one.

Until 0.6.1 the catalog was in the repository and nowhere else. `pip install bounded-loops`
gave you an engine and no loops: `bl loops list` walked the filesystem, found nothing, and the
README had to tell people to clone the repo to obtain the thing the package advertises. The
wheel now carries the catalog at ``bounded_loops/catalog/loops/``.

**Why the catalog is bundled rather than downloaded.** A `bl loops install` that fetched from
github.com would be unavailable in exactly the environments this engine is aimed at —
air-gapped, corporate-proxied, CI without egress. The engine's whole posture is that it runs
offline with no credential; a catalog that needed the network would contradict it. 2.5 MB in
the wheel is the cheaper honesty.

**Why installing is still a separate step.** `bl run` writes its ledger BESIDE the loop, so a
loop must live somewhere writable. `site-packages` is neither writable in a managed
environment nor a sensible place to accumulate run receipts, and mutating it would make one
user's run visible to every project on the machine. `bl loops install` copies a loop into the
project workspace, which is the only place a run's evidence belongs.
"""

from __future__ import annotations

import shutil
from importlib.resources import files
from pathlib import Path

#: Files a run leaves behind. Never copied into a fresh install: a new loop must start from
#: the package's own state, not inherit a ledger describing somebody else's run.
_RUN_ARTIFACTS = frozenset({".ledger.jsonl", ".bounded-loops", "__pycache__", ".STATE.md.runtime"})


def bundled_catalog_root() -> Path | None:
    """The catalog shipped INSIDE the wheel, or None if this is not an installed wheel.

    Distinct from `catalog_root()` on purpose: the release contract test needs to assert that
    the wheel really carries the catalog, and a function that silently falls back to the
    source tree could never fail that assertion — it would pass on a developer's machine for
    the wrong reason.
    """
    try:
        root = files("bounded_loops").joinpath("catalog", "loops")
    except (ModuleNotFoundError, TypeError):
        return None
    try:
        path = Path(str(root))
    except (TypeError, ValueError):
        return None
    return path if path.is_dir() else None


def _source_checkout_catalog() -> Path | None:
    """The repository's own ``loops/``, when running from a checkout."""
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        candidate = ancestor / "loops"
        if candidate.is_dir() and any(candidate.glob("*/loop.yaml")):
            return candidate
    return None


def catalog_root() -> Path | None:
    """The catalog THIS installation can offer, wherever it lives.

    Bundled first, then the source tree. A developer working in a checkout should be able to
    `bl loops install` exactly like an end user — the catalog is the same catalog, and having
    the command work for only one of them is how a feature reaches release untested by the
    people building it.
    """
    return bundled_catalog_root() or _source_checkout_catalog()


#: Kept so existing callers and tests keep working; `catalog_root` is the one to use.
packaged_catalog_root = catalog_root


def packaged_loop_names() -> list[str]:
    """Every loop name this installation can offer, sorted. Empty when there is no catalog."""
    root = catalog_root()
    if root is None:
        return []
    return sorted(
        entry.name
        for entry in root.iterdir()
        if entry.is_dir() and (entry / "loop.yaml").is_file()
    )


def _skip_run_artifacts(_directory: str, names: list[str]) -> set[str]:
    """`shutil.copytree` ignore callback: leave every run artifact behind."""
    return {name for name in names if name in _RUN_ARTIFACTS}


def install_loop(name: str, destination: Path, *, overwrite: bool = False) -> Path:
    """Copy the bundled loop `name` to ``destination/name`` and return that path.

    Raises `LookupError` when the catalog or the loop is not present, and `FileExistsError`
    when the target exists and `overwrite` is False — refusing rather than merging, because a
    half-overwritten loop is a manifest that no longer matches its own seed.
    """
    root = catalog_root()
    if root is None:
        raise LookupError("this installation has no loop catalog to install from")
    # `name` reaches an rmtree below, so it is validated as ONE ordinary path segment before
    # it is ever joined. A name like "../.." would otherwise resolve outside the destination
    # and delete a directory the caller never mentioned.
    if name != Path(name).name or name in {"", ".", ".."} or "/" in name or "\\" in name:
        raise LookupError(f"{name!r} is not a loop name")

    source = root / name
    if not (source / "loop.yaml").is_file():
        raise LookupError(f"no loop named {name!r} in the bundled catalog")

    target = destination / name
    if target.exists():
        if not overwrite:
            raise FileExistsError(str(target))
        # Only ever remove something that is itself a loop package. If the target is not one,
        # the caller has pointed at the wrong directory and deleting it would be the mistake.
        if not (target / "loop.yaml").is_file():
            raise FileExistsError(
                f"{target} exists and is not a loop package — refusing to replace it"
            )
        shutil.rmtree(target)

    destination.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, ignore=_skip_run_artifacts)
    return target
