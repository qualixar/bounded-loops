"""
AnchorGuardRunner — a RunnerPort decorator enforcing workspace integrity for
EVERY runner, so a gate cannot be tricked into passing by tampering with what
it verifies.

Background — why the per-runner StubRunner forbid check was necessary but NOT
sufficient:
  1. `forbid` was only enforced inside StubRunner. A shell/codex/claude/
     python_callable agent could `echo 'assert True' > seed/test_slugify.py`
     and no engine code blocked it.
  2. StubRunner's `fnmatch` was case-sensitive, so on a case-insensitive
     filesystem (macOS APFS) a cassette writing `Seed/test_slugify.py`
     resolved to the same file yet dodged the `seed/test_*.py` glob.
  3. Protecting the anchor FILE's bytes does not protect the anchor's ROLE: a
     cassette could leave `seed/test_slugify.py` untouched but plant a
     `pyproject.toml`/`pytest.ini`/`conftest.py` that redirects pytest
     collection away from it, so the real (still-failing) test is never run
     and the gate sees `1 passed`.

This guard closes all three at the engine level, once, for all runners:
  - Snapshots, at wire time, the content hash of every workspace file matching
    the loop's `forbid` globs (CASE-INSENSITIVE) — the protected anchors.
  - Snapshots the collection-redirect sentinels (pyproject.toml, pytest.ini,
    tox.ini, setup.cfg, and every conftest.py) present at baseline.
  - After EVERY runner turn, re-scans and raises RunnerError if any anchor was
    modified/removed, any NEW file matching a forbid glob appeared, or any
    collection sentinel was added or changed. RunnerError propagates as a
    BoundedLoopsError (non-DONE, non-zero exit) — the loop NEVER reaches the
    gate against a tampered workspace, so it can never report a false DONE.

Anchor files whose mutation IS the fix (e.g. a lockfile in an osv loop) must
NOT be listed in `forbid`; the "nothing to scan" false-pass for those is
closed in the gate adapters (osv/checkov) instead, not here.
"""
from __future__ import annotations

import fnmatch
import hashlib
from pathlib import Path

from bounded_loops.domain.errors import RunnerError
from bounded_loops.domain.models import LoopContext, RunResult, Spec

# Files that can silently redirect what a pytest (or pytest-via-command) gate
# collects. An agent fixing seed code never needs to create these in the
# sandbox; a NEW or MODIFIED one mid-run is always a collection-redirect attempt.
_COLLECTION_SENTINELS = frozenset({
    "pyproject.toml", "pytest.ini", "tox.ini", "setup.cfg", "conftest.py",
})


def matches_forbid(rel_path: str, forbid: tuple[str, ...]) -> bool:
    """Case-insensitive fnmatch of a workspace-relative POSIX path (or its
    basename) against the loop's forbid globs. Case-insensitive closes the
    macOS APFS `Seed/` vs `seed/` bypass."""
    rel_low = rel_path.lower()
    base_low = rel_path.rsplit("/", 1)[-1].lower()
    for pattern in forbid:
        pat = pattern.lower()
        if fnmatch.fnmatch(rel_low, pat) or fnmatch.fnmatch(base_low, pat):
            return True
    return False


def _hash_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "<unreadable>"


def _scan(workspace: Path, forbid: tuple[str, ...]) -> tuple[dict[str, str], dict[str, str]]:
    """Return ({forbid-matched rel_path: hash}, {sentinel rel_path: hash}) for
    the current workspace tree."""
    anchors: dict[str, str] = {}
    sentinels: dict[str, str] = {}
    ws = workspace.resolve()
    for p in ws.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(ws).as_posix()
        if matches_forbid(rel, forbid):
            anchors[rel] = _hash_file(p)
        if p.name.lower() in _COLLECTION_SENTINELS:
            sentinels[rel] = _hash_file(p)
    return anchors, sentinels


class AnchorGuardRunner:
    """Wraps a RunnerPort; verifies workspace integrity after every turn."""

    def __init__(self, inner, workspace: Path, forbid: tuple[str, ...],
                 protect_collection_config: bool = True) -> None:
        self._inner = inner
        self._workspace = workspace
        self._forbid = tuple(forbid)
        # Collection-config protection only makes sense for pytest-style gates
        # (composition sets this from the gate kind). For a non-pytest loop, a
        # pyproject.toml/conftest.py is irrelevant to the gate, so creating one
        # is not an attack and must not be blocked.
        self._protect_config = protect_collection_config
        # Baseline snapshot taken at construction — AFTER the scratch workspace
        # is built (composition wires this decorator post-_make_scratch_workspace).
        self._base_anchors, self._base_sentinels = _scan(workspace, self._forbid)

    def run_once(self, spec: Spec, ctx: LoopContext) -> RunResult:
        result = self._inner.run_once(spec, ctx)
        self._verify()
        return result

    def _verify(self) -> None:
        anchors, sentinels = _scan(self._workspace, self._forbid)

        # 1. A protected anchor removed or its content changed.
        for rel, base_hash in self._base_anchors.items():
            if rel not in anchors:
                raise RunnerError(
                    f"SECURITY: the gate's verification anchor {rel!r} was DELETED "
                    f"from the workspace during the run. Refusing to run the gate "
                    f"against a tampered workspace."
                )
            if anchors[rel] != base_hash:
                raise RunnerError(
                    f"SECURITY: the gate's verification anchor {rel!r} was MODIFIED "
                    f"during the run (it is declared in the loop's forbid list). A "
                    f"runner may not neuter the file the gate checks — refusing to "
                    f"report a pass on a tampered anchor."
                )

        # 2. A NEW file matching a forbid glob appeared (e.g. a planted
        #    seed/test_evil.py under forbid: seed/test_*.py).
        for rel in anchors:
            if rel not in self._base_anchors:
                raise RunnerError(
                    f"SECURITY: a new file {rel!r} matching the loop's forbid list "
                    f"was created during the run — refusing to proceed."
                )

        # 3. A collection-redirect sentinel added or changed — only
        #    enforced for pytest-style gates.
        if not self._protect_config:
            return
        for rel, h in sentinels.items():
            if rel not in self._base_sentinels:
                raise RunnerError(
                    f"SECURITY: a test-collection config file {rel!r} "
                    f"(pyproject.toml/pytest.ini/conftest.py/…) was planted in the "
                    f"workspace during the run. This can redirect the gate away from "
                    f"the real verification anchor — refusing to proceed."
                )
            if self._base_sentinels[rel] != h:
                raise RunnerError(
                    f"SECURITY: the test-collection config file {rel!r} was modified "
                    f"during the run — refusing to proceed (possible collection redirect)."
                )
