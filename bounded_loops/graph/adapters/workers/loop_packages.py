"""Content-addressed loop packages, and the resolver that makes ``kind: loop`` runnable.

``loop_package`` was a required, digest-shaped field on every ``kind: loop`` node from the start —
validated at authoring time, copied into ``PlannedNode.package_digest`` at compile time, and then
never used again. Nothing produced that digest from real files and nothing re-checked it before
execution, so the field named a package while the node ran whatever its connector binding pointed
at. This module closes that loop: it computes the digest from the package's own bytes, resolves a
digest back to a directory, and turns the result into the ``NodeExecutionSpec`` the sandboxed worker
already knows how to run.

Resolution is BY DIGEST ONLY. A name lookup would mean that pulling new commits silently changes
what a persisted ``plan_id`` executes, and ``plan_id`` is the value that decides whether an existing
run directory may be resumed.
"""

from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from bounded_loops.graph.adapters.workers.sandboxed_worker import NodeExecutionSpec
from bounded_loops.graph.application.workspace_promotion import WorkspaceInput
from bounded_loops.graph.domain.errors import GraphIntegrityError
from bounded_loops.graph.domain.plan import PlannedNode

#: The single declared output of a loop node, written by ``graph.loop_node_entry`` into its cwd and
#: promoted by the sandboxed worker. Declared HERE rather than in the entry point because the
#: resolver owns the ``declared_outputs`` contract — the entry point is the consumer of it, and
#: pointing the dependency the other way made these two modules import each other.
DEFAULT_OUTCOME_FILENAME = "loop-outcome.json"
#: Loop-engine storage lives under the node workspace, never inside the read-only package.
CONTROLLER_SUBDIR = ".controller"

#: Directory and file names EXCLUDED from a package digest.
#:
#: The selection rule is that every entry is something a RUN can create inside a package directory.
#: Covering too little is the dangerous direction: digest only ``loop.yaml`` and someone can edit
#: ``seed/`` or the gate's own checker script, and a resumed ``plan_id`` then executes different code
#: while the receipt still names the digest that was recorded. Covering too much is merely annoying —
#: a single ``bl run`` that wrote into the package would move the digest and make existing runs
#: unresumable — and ``.bounded-loops`` is exactly that case, which is why it heads the list.
#:
#: **The limit this creates, stated because an earlier version of this comment asserted that these
#: names are "never something the package ships" with nothing enforcing it.** Nothing in this module
#: can distinguish a ``__pycache__`` that a run produced from one an author committed, so content
#: under an excluded name is INVISIBLE to the digest: a package must not ship code there, and a
#: package that does has a mutable region under a pinned digest. For the 68 packages this repository
#: ships that rule is enforced — ``test_no_shipped_loop_package_hides_content_under_an_excluded_name``
#: fails CI if any of them acquires one. For a third-party package it is a contract with the author,
#: which is why it is written down here rather than implied.
#:
#: ``.ledger.jsonl`` was MISSED until CI caught it, and the way it was caught is the lesson. The
#: README's own quickstart is ``bl run loops/bug-fix-red-green``, which writes
#: ``loops/bug-fix-red-green/.ledger.jsonl``. Every machine that has followed the quickstart therefore
#: digests that package differently from a fresh clone — so the committed reference-graph digests,
#: generated on such a machine, did not match a clean checkout and `tests/graphs` failed on CI while
#: passing locally. A run artifact inside the package is exactly what this list is for, and the local
#: suite could not see the gap because the dirty state was the same in both places.
#:
#: Note ``STATE.md`` is deliberately NOT excluded. It is the loop's memory seed, so it can change
#: behaviour, and anything that changes behaviour belongs in the digest. Graph runs cannot dirty it:
#: ``wire_loop_for_graph`` refuses a controller root inside the package.
_EXCLUDED_NAMES = frozenset({
    ".bounded-loops",   # loop-engine run storage, written by `bl run` without a controller root
    ".ledger.jsonl",    # the loop's own event log, written by `bl run` INTO the package directory
    ".trust.json",      # per-loop gate-command trust record, same writer, same place
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".git",
    ".DS_Store",
})
_EXCLUDED_SUFFIXES = (".pyc", ".pyo")


def _digestible_entries(package: Path) -> Iterable[Path]:
    """Every file AND directory the digest covers, in sorted relative-path order.

    Directories are yielded because an EMPTY one is observable: ``shutil.copytree`` reproduces it
    into the workspace, so a gate whose ``run:`` branches on ``test -d seed/hidden_branch`` changes
    verdict while a files-only digest stays fixed. That is a mutable region under a pinned digest,
    which is the exact failure this module exists to prevent (found by the P4.5 audit, Grok 7).
    """
    for path in sorted(package.rglob("*")):
        if any(part in _EXCLUDED_NAMES for part in path.relative_to(package).parts):
            continue
        if path.is_symlink():
            # A symlink's TARGET is outside the digest's reach, so hashing the link would certify
            # bytes this function never read. Refuse rather than certify a package whose content
            # depends on something the digest cannot cover. Checked before `is_dir()`/`is_file()`,
            # both of which FOLLOW the link and would classify it as ordinary content.
            raise GraphIntegrityError(
                f"loop package {package} contains a symlink ({path.relative_to(package)}); "
                "a package must be self-contained to be content-addressed"
            )
        if path.is_dir():
            yield path
            continue
        if path.is_file():
            if not path.name.endswith(_EXCLUDED_SUFFIXES):
                yield path
            continue
        # Everything else is a FIFO, socket, or device node. ``is_file()`` is False for all of them,
        # so the previous version simply skipped them — and a package could carry
        # ``seed/payload_fifo`` that a gate's ``run:`` branches on with ``test -p`` while the pinned
        # digest never moved. Same defect class as a symlink and the same answer: refuse, rather than
        # certify a package whose behaviour depends on something the digest does not cover. Found by
        # the P4.5 round-2 audit (Muse 3-1), which demonstrated it with ``os.mkfifo``.
        raise GraphIntegrityError(
            f"loop package {package} contains a non-regular file "
            f"({path.relative_to(package)}); a package must be plain files and directories to be "
            "content-addressed"
        )


#: Entry-kind tags in the canonical form. Present so a directory named ``x`` and an empty file named
#: ``x`` cannot produce the same bytes — without the tag, a file of length 0 and a directory would
#: both reduce to their path alone.
_DIR_TAG = "d"
_FILE_TAG = "f"


def loop_package_digest(package: Path) -> str:
    """Return the canonical content digest of a loop package directory.

    Canonical form: for each included entry in sorted relative-path order, a kind tag, the POSIX
    relative path, and — for files — an executable-bit flag, the byte length, and the bytes. Each
    variable-length field is length-prefixed, so no combination of paths and contents can be
    re-partitioned into a different package with the same digest.

    Mtimes and uids are excluded on purpose, and so is every mode bit except ``0o111``. Git preserves
    exactly one permission bit, so hashing the rest would make the digest depend on the checking-out
    user's umask rather than on what the package contains, and a fresh clone of the same commit would
    compute a different digest and refuse to resume its own runs. The cost of that choice is real and
    worth naming: a package whose behaviour depends on a file being unreadable, or on a setuid bit,
    has a mutable region under a pinned digest. Loop packages are inputs to a gate, not a permission
    system, so the trade goes to reproducibility.
    """
    package = package.resolve()
    if not package.is_dir():
        raise GraphIntegrityError(f"loop package {package} is not a directory")
    accumulator = hashlib.sha256()
    for path in _digestible_entries(package):
        relative = path.relative_to(package).as_posix()
        if path.is_dir():
            accumulator.update(f"{_DIR_TAG}:{len(relative)}:{relative}:".encode("utf-8"))
            continue
        payload = path.read_bytes()
        # The executable bit is part of the execution surface: a gate's `run:` may invoke a shipped
        # script directly, and whether that script is executable changes whether the gate can run.
        executable = "1" if path.stat().st_mode & 0o111 else "0"
        header = (
            f"{_FILE_TAG}:{len(relative)}:{relative}:{executable}:{len(payload)}:"
        ).encode("utf-8")
        accumulator.update(header)
        accumulator.update(payload)
    return accumulator.hexdigest()


#: The authoring schema requires ``loop_package`` to match ``^sha256:[0-9a-f]{64}$`` (see
#: ``validate_graph._DIGEST``), and the compiler copies that string verbatim into
#: ``PlannedNode.package_digest``. ``loop_package_digest`` returns the BARE hex, because it is a
#: digest function and the prefix is a vocabulary choice of the graph schema. Both forms must
#: therefore resolve, or an authored graph would never find its own package — which is exactly what
#: the first end-to-end run showed.
_ALGORITHM_PREFIX = "sha256:"


def normalise_package_digest(digest: str) -> str:
    """Return the bare hex form of a package digest, accepting the ``sha256:`` prefixed form."""
    return digest[len(_ALGORITHM_PREFIX):] if digest.startswith(_ALGORITHM_PREFIX) else digest


def qualified_package_digest(package: Path) -> str:
    """The package digest in the ``sha256:``-prefixed form a graph manifest must declare."""
    return f"{_ALGORITHM_PREFIX}{loop_package_digest(package)}"


@dataclass(frozen=True)
class LoopPackageRegistry:
    """Maps an admitted package digest to a directory on this host, and re-verifies the bytes.

    ``roots`` are directories whose immediate children are candidate loop packages (the shipped
    ``loops/`` tree, plus whatever a deployment adds). The index is built by digesting each
    candidate, so a package is reachable only if its CONTENT hashes to the digest the plan admitted.
    """

    roots: tuple[Path, ...]

    def index(self) -> Mapping[str, Path]:
        found: dict[str, Path] = {}
        for root in self.roots:
            if not root.is_dir():
                continue
            for entry in sorted(root.iterdir()):
                if not (entry / "loop.yaml").is_file():
                    continue
                # RESOLVED, always. The path this returns is handed to a subprocess that runs with a
                # DIFFERENT cwd (the node's own output directory), so a relative path here becomes a
                # package that does not exist there. That is exactly how the first end-to-end graph
                # run failed: a relative root resolved fine while digesting and then vanished inside
                # the sandbox, and the node reported a bare "worker execution failed".
                candidate = entry.resolve()
                digest = loop_package_digest(candidate)
                first = found.get(digest)
                if first is not None and first != candidate:
                    # Byte-identical packages under two names are indistinguishable by digest, so
                    # resolution would depend on iteration order. Refuse rather than pick one.
                    raise GraphIntegrityError(
                        f"two loop packages share digest {digest}: {first} and {candidate}"
                    )
                found[digest] = candidate
        return found

    def resolve(self, digest: str) -> Path:
        package = self.index().get(normalise_package_digest(digest))
        if package is None:
            raise GraphIntegrityError(
                f"no loop package on this host has digest {digest}; "
                f"searched {', '.join(str(root) for root in self.roots) or '(no roots configured)'}"
            )
        return package


@dataclass(frozen=True)
class LoopNodeResolver:
    """Turns a ``kind: loop`` node into the argv that runs its package under the node's sandbox.

    This is the ``NodeExecutionResolver`` the sandboxed worker's own docstring anticipated —
    "backed by an admitted package registry in production". Filling it is what makes a loop node
    executable; the worker already supplies the sandbox, the deadline, the rlimits and the
    descriptor-safe promotion of exactly the declared outputs.
    """

    registry: LoopPackageRegistry
    run_id: str
    #: Attempt and round come from the controller per attempt, so the resolver is rebuilt rather
    #: than mutated — a resolver that carried mutable attempt state would be a race waiting to
    #: happen once nodes run concurrently.
    attempt: int = 1
    repair_round: int = 0
    interpreter: str = sys.executable
    #: Input artifacts to materialize in BL_GRAPH_INPUTS before the loop process starts.
    #: Empty by default so fixtures (no declared ports) are unchanged.
    input_artifacts: tuple[WorkspaceInput, ...] = ()
    #: Additional declared outputs beyond loop-outcome.json, one per named output port.
    #: Each item is (relative_path, media_type).  Paths use "outputs/<port_name>" so they
    #: sort AFTER "loop-outcome.json", preserving the LoopReceiptGate digests[0] invariant.
    extra_declared_outputs: tuple[tuple[str, str], ...] = ()

    def resolve(self, node: PlannedNode) -> NodeExecutionSpec:
        if node.package_digest is None:
            raise GraphIntegrityError(
                f"node {node.node_id!r} is a loop node with no package digest; "
                "authoring requires loop_package and the compiler carries it into the plan"
            )
        package = self.registry.resolve(node.package_digest)
        declared: dict[str, str] = {DEFAULT_OUTCOME_FILENAME: "application/json"}
        for path, media_type in self.extra_declared_outputs:
            declared[path] = media_type
        return NodeExecutionSpec(
            argv=(
                self.interpreter, "-I", "-B", "-m", "bounded_loops.graph.loop_node_entry",
                "--package", str(package),
                "--package-digest", node.package_digest,
                "--run-id", self.run_id,
                "--node-id", node.node_id,
                "--attempt", str(self.attempt),
                "--repair-round", str(self.repair_round),
                "--outcome", DEFAULT_OUTCOME_FILENAME,
            ),
            declared_outputs=declared,
            inputs=self.input_artifacts,
        )
