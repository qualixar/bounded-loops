"""Content-addressed change detection for a governed workspace.

WHY THIS REPLACES THE GIT CHECK
-------------------------------
Change detection used to be ``git status --porcelain`` against the scratch workspace, which
``composition`` git-init'd and committed exactly once at wire time. Nothing ever committed again,
and ``ShellRunner`` writes ``agent_output.txt`` into that same workspace on every lap. From lap 2
onward that one untracked file made porcelain non-empty, so ``changed`` was ``True``
unconditionally — and the no-progress soft bound, which fires only on a run of ``changed == False``
laps, could never fire at all under that runner.

The defect survived because neither instrument pointed at it could see it: the no-progress unit
test injects a fake runner returning ``changed=False`` directly, bypassing detection entirely, and
no catalogue loop ever reaches lap 2. It needed a git fixture no test built.

Hashing content instead:

* is deterministic and testable in-process, with no git fixture and no subprocess;
* mutates nothing, so the workspace snapshot stays pristine;
* retires git as a *hard* engine dependency of the bound (``composition`` previously refused to
  run without it, on the strength of this check alone).

WHAT COUNTS AS A CHANGE
-----------------------
Agent work product only. Files the harness itself writes into the agent's workspace are
bookkeeping and are excluded by name — reading our own log as evidence of the agent's progress is
precisely the bug above.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

#: Files the HARNESS writes into the agent-visible workspace. Bookkeeping, never work product.
#: Excluded by name so that a future change writing one of them EARLIER in the lap cannot
#: resurrect the defect this module exists to fix.
HARNESS_ARTIFACTS = frozenset({
    "agent_output.txt",
    ".bounded-loops-scratch",
    ".ledger.jsonl",
    ".STATE.md.runtime",
})

#: Directories excluded wholesale. ``.git`` matters most: the scratch copy is still git-init'd, and
#: object/index churn there is not the agent editing the workspace.
_EXCLUDED_DIRS = frozenset({
    ".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
})

_CHUNK_BYTES = 1 << 16


def _is_excluded(path: Path, workspace: Path) -> bool:
    relative = path.relative_to(workspace)
    if relative.name in HARNESS_ARTIFACTS:
        return True
    return any(part in _EXCLUDED_DIRS for part in relative.parts)


def _entry_digest(path: Path) -> bytes:
    """Digest one entry's content.

    A symlink contributes its TARGET STRING, never the target's content: following it would read
    outside the workspace, which is the sandbox boundary the whole isolation story rests on.

    An unreadable entry contributes a deterministic marker rather than being skipped. Skipping
    would make an unreadable file invisible to the digest, so a permissions change would read as
    "the agent did nothing" — the same silent-agreement failure this module was written to remove.
    """
    if path.is_symlink():
        return b"symlink:" + str(path.readlink()).encode("utf-8", "surrogateescape")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(_CHUNK_BYTES):
                digest.update(chunk)
    except OSError as exc:
        return b"unreadable:" + type(exc).__name__.encode("ascii")
    return b"sha256:" + digest.digest()


def workspace_digest(workspace: Path) -> str:
    """A stable hex digest over the agent-visible content of ``workspace``.

    Two calls return the same value if and only if every agent-visible path and its content are
    unchanged. Directory-iteration order does not affect the result; paths are sorted. Path names
    are hashed alongside content, so moving a file to a new name is a change even though the bytes
    are identical.
    """
    workspace = Path(workspace)
    if not workspace.is_dir():
        return "absent"

    overall = hashlib.sha256()
    for path in sorted(workspace.rglob("*")):
        if _is_excluded(path, workspace):
            continue
        relative = path.relative_to(workspace).as_posix().encode("utf-8", "surrogateescape")
        overall.update(relative)
        overall.update(b"\x00")
        # A directory contributes its name only. An empty directory appearing or disappearing is
        # therefore still a change, which a content-only digest would miss.
        overall.update(b"dir" if path.is_dir() and not path.is_symlink() else _entry_digest(path))
        overall.update(b"\x00")
    return overall.hexdigest()
