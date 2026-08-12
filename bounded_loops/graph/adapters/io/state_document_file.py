"""Persist a rendered STATE.md to disk.

STATE.md is a regenerated UX projection (ADR-12 D4), so the writer OVERWRITES atomically —
a fresh temp file in the same directory then ``os.replace`` — never appends, so a reader
never sees a half-written document and a crash never corrupts the last-good one. It refuses a
symlinked target (the same path-safety lesson as the durable KV backend) and holds no authority.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile

from bounded_loops.graph.domain.errors import GraphIntegrityError


def write_state_document(path: Path, markdown: str) -> None:
    """Atomically write ``markdown`` to ``path`` (UTF-8), overwriting any existing file.
    Refuses a symlinked target fail-closed."""
    if path.is_symlink():
        raise GraphIntegrityError("STATE.md path must not be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    # A unique temp in the SAME directory guarantees os.replace is an atomic rename (same
    # filesystem), and avoids racing/following a stale or symlinked fixed temp name.
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".state-", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(markdown)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
