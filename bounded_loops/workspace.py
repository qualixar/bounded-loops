"""The project workspace — the one answer to "where does this project keep its runs".

A workspace is a `.bounded-loops/` directory living beside the code it serves, the same way
`.git/` and `.claude/` do. Every surface — the `bl` CLI, the MCP server, the Jarvis UI — resolves
it through *this* module. Two implementations of "where do receipts go" is the
second-source-of-truth defect class that two audit rounds were full of, so there is one.

Layout::

    .bounded-loops/
      config.toml            connections, egress posture, spend caps, loop roots
      graphs/<name>.yaml     the project's own graph manifests
      loops/<name>/          the project's own loop packages
      runs/<run_id>/         run directories — layout UNCHANGED from 0.4.0
      tickets/<id>.md        human work items ("this node has no mechanical check yet")
      index.json             a run/ticket listing CACHE

`index.json` is a **cache**. It is derived from `runs/` and must be rebuildable by scanning it.
Nothing may consult it as authority: the append-only hash-chained receipt log in each run
directory is the only durable truth in this system, and a second reader of one fact is free to
disagree with the first.

Discovery is *pure* — it reports where the workspace is or would go, and never creates anything.
`ensure()` creates.

Additive by design: no existing run directory moves. `bl run --run-id X` keeps writing
`<loop-package>/.bounded-loops/runs/X`, because `--resume` and `bl runs` read from there and
every 0.4.0/0.5.x run must stay resumable. Only the graph surface — whose `--out` had no default
at all — gains one. See `storage_root_for_loop`.
"""

from __future__ import annotations

import os
import secrets
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal, Mapping

from bounded_loops.application.run_store import validate_run_id
from bounded_loops.domain.errors import ManifestError

WORKSPACE_DIRNAME = ".bounded-loops"
WORKSPACE_ENV_VAR = "BOUNDED_LOOPS_WORKSPACE"
CONFIG_FILENAME = "config.toml"
CONFIG_VERSION = 1

Origin = Literal["existing", "git-toplevel", "cwd", "explicit"]

_ORIGIN_REASONS: Mapping[str, str] = {
    "existing": f"an existing {WORKSPACE_DIRNAME}/ was found at or above the starting directory",
    "git-toplevel": f"no {WORKSPACE_DIRNAME}/ exists yet, so the git repository root was chosen",
    "cwd": f"no {WORKSPACE_DIRNAME}/ exists and this is not a git repository, so the "
    "starting directory was chosen",
    "explicit": f"it was named explicitly (argument or ${WORKSPACE_ENV_VAR})",
}

_DEFAULT_CONFIG = f"""\
# bounded-loops project workspace.
# Created by `bl init`. Safe to edit and safe to commit — it holds NO secrets.
# Credentials never live here: connections resolve through the no-secret broker.

[workspace]
version = {CONFIG_VERSION}

# Extra directories to search for loop packages, in addition to this workspace's loops/.
# [catalog]
# loop_roots = ["./loops"]

# Default spend ceiling applied to a run when the manifest does not set one.
# [budget]
# max_tokens = 200000
# max_cost_microunits = 5000000

# Egress posture for connector nodes. The installer writes ~/.bounded-loops/egress.json;
# this section, when present, narrows it for this project only.
# [egress]
# posture = "deny"

# The Stop hook refuses to let a host agent say "done" while a run here is not in a terminal
# state. That is the point of the tool, and it is also a behaviour change in your editor — so it
# has an off switch, and the switch lives in YOUR project, not in our plugin files. Set false to
# get a warning instead of a refusal.
# [hooks]
# stop_on_active_run = true
"""


@dataclass(frozen=True)
class Workspace:
    """A resolved workspace: the project root, and why this root was chosen.

    Every path below is *derived* from `project_root`, never stored separately — a stored copy
    is a second source of truth waiting to drift. `index_path` names a rebuildable cache, not
    an authority.
    """

    project_root: Path
    origin: Origin

    @property
    def root(self) -> Path:
        return self.project_root / WORKSPACE_DIRNAME

    @property
    def config_path(self) -> Path:
        return self.root / CONFIG_FILENAME

    @property
    def graphs_dir(self) -> Path:
        return self.root / "graphs"

    @property
    def loops_dir(self) -> Path:
        return self.root / "loops"

    @property
    def runs_dir(self) -> Path:
        return self.root / "runs"

    @property
    def tickets_dir(self) -> Path:
        return self.root / "tickets"

    @property
    def index_path(self) -> Path:
        """A rebuildable cache of runs and tickets. Never authority."""
        return self.root / "index.json"

    @property
    def reason(self) -> str:
        """Plain-language explanation of `origin`, for `bl where` and the UI."""
        return _ORIGIN_REASONS[self.origin]

    def run_dir(self, run_id: str) -> Path:
        """The directory for one run.

        Reuses `run_store.validate_run_id` rather than re-deriving what a safe run id is: a
        second validator is a second answer, and the weaker of the two becomes the hole.
        """
        return self.runs_dir / validate_run_id(run_id)

    def exists(self) -> bool:
        return self.root.is_dir()


# ── discovery ────────────────────────────────────────────────────────────────


def discover(start: Path | None = None, *, explicit: Path | None = None) -> Workspace:
    """Resolve the workspace for `start` (default: the current directory).

    Order of precedence:

    1. `explicit`, then `$BOUNDED_LOOPS_WORKSPACE`.
    2. The nearest existing `.bounded-loops/` at or above `start`, bounded by the git
       repository root — a repository's receipts must not silently land in a workspace that
       happens to sit above the checkout.
    3. The git repository root, if there is one.
    4. `start`.

    Creates nothing. Raises `ManifestError` if a candidate `.bounded-loops` exists but is a
    symlink or is not a directory.
    """
    named = explicit if explicit is not None else _from_environment()
    if named is not None:
        workspace = Workspace(project_root=Path(named).resolve(), origin="explicit")
        _inspect(workspace.root)
        return workspace

    origin_start = (start if start is not None else Path.cwd()).resolve()
    ceiling = _repository_root(origin_start)

    for candidate in _upward(origin_start, ceiling):
        workspace = Workspace(project_root=candidate, origin="existing")
        if _inspect(workspace.root):
            return workspace

    if ceiling is not None:
        return Workspace(project_root=ceiling, origin="git-toplevel")
    return Workspace(project_root=origin_start, origin="cwd")


def _from_environment() -> Path | None:
    raw = os.environ.get(WORKSPACE_ENV_VAR, "").strip()
    return Path(raw) if raw else None


def _upward(start: Path, ceiling: Path | None) -> Iterator[Path]:
    """Yield `start` and its parents, stopping after `ceiling` when there is one."""
    for candidate in (start, *start.parents):
        yield candidate
        if ceiling is not None and candidate == ceiling:
            return


def _repository_root(start: Path) -> Path | None:
    """The nearest ancestor holding a `.git` entry, or None.

    Detected by presence rather than by shelling out to `git rev-parse`: no subprocess on a
    path every CLI invocation takes, no dependency on git being installed, and it still works
    for a linked worktree or a submodule, where `.git` is a file rather than a directory.
    """
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _inspect(root: Path) -> bool:
    """True if `root` is a usable workspace directory, False if absent; raise if unusable."""
    if root.is_symlink():
        raise ManifestError(
            f"{root} must not be a symlink — a symlinked workspace silently relocates every "
            "receipt in this project; point $" + WORKSPACE_ENV_VAR + " at the real directory "
            "instead"
        )
    if not root.exists():
        return False
    if not root.is_dir():
        raise ManifestError(f"{root} exists but is not a directory")
    return True


# ── creation and configuration ───────────────────────────────────────────────


def ensure(workspace: Workspace) -> list[Path]:
    """Create any missing part of the layout. Returns exactly what it created.

    Idempotent, and never overwrites `config.toml` — the config is the user's file, so it is
    written with an exclusive create and left alone if it is already there.
    """
    created: list[Path] = []
    for directory in (
        workspace.root,
        workspace.graphs_dir,
        workspace.loops_dir,
        workspace.runs_dir,
        workspace.tickets_dir,
    ):
        if not directory.exists():
            created.append(directory)
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ManifestError(f"cannot create {directory}: {exc}") from exc

    try:
        with workspace.config_path.open("x", encoding="utf-8") as handle:
            handle.write(_DEFAULT_CONFIG)
        created.append(workspace.config_path)
    except FileExistsError:
        pass
    except OSError as exc:
        raise ManifestError(f"cannot write {workspace.config_path}: {exc}") from exc

    return created


def read_config(workspace: Workspace) -> Mapping[str, Any]:
    """Read `config.toml`. Missing file reads as empty; malformed TOML is a clear refusal."""
    path = workspace.config_path
    if path.is_symlink():
        raise ManifestError(f"{WORKSPACE_DIRNAME}/{CONFIG_FILENAME} must not be a symlink")
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ManifestError(
            f"{WORKSPACE_DIRNAME}/{CONFIG_FILENAME} is unreadable: {exc}"
        ) from exc
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(
            f"{WORKSPACE_DIRNAME}/{CONFIG_FILENAME} is not valid TOML: {exc}"
        ) from exc


def mint_run_directory_name(*, now: datetime | None = None) -> str:
    """A fresh, sortable, collision-resistant directory name for one run.

    This names a *directory*, not the engine's `run_id`: the single-tenant CLI uses a fixed
    `LOCAL_RUN_ID` as its identity (it shapes plan and approval-ledger derivation), while the
    out directory is what distinguishes one execution from the next. `graph_composition`
    refuses an out directory that already holds a run, so the name must be unique — the random
    suffix is there because two runs started inside the same second are ordinary.

    Shaped to satisfy `run_store.validate_run_id`, so it is a legal `Workspace.run_dir` key.
    """
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(3)}"


# ── the seam to the loop run store ───────────────────────────────────────────


def storage_root_for_loop(
    workspace: Workspace,
    loop_dir: Path,
    *,
    opt_in: bool = False,
) -> Path | None:
    """The `storage_root` a *loop* run should hand to `run_store`, or None for the default.

    None means "package-local" — `<loop-package>/.bounded-loops/runs/<run_id>`, which is where
    every 0.4.0/0.5.x loop run already lives. That is the default on purpose: redirecting loop
    receipts into the project workspace would strand every existing run from `--resume` and
    `bl runs`, and the release decision is additive.

    `opt_in=True` asks for the project workspace instead. Even then this returns None when the
    workspace sits inside the loop package, because `run_store._runs_root` refuses a storage
    root under the package it is storing for — a legal layout must not become a crash.

    Returns `workspace.root`, not `runs_dir`: `run_store` appends `runs/` itself.
    """
    if not opt_in:
        return None
    package_root = loop_dir.resolve()
    root = workspace.root.resolve()
    if root == package_root or root.is_relative_to(package_root):
        return None
    return root
