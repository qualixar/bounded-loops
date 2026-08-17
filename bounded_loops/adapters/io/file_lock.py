"""One advisory file-lock primitive, shared by every append-only evidence writer.

Before this module the same twenty lines existed three times: in
``hash_chain_events.py``, in ``graph/adapters/persistence/event_log.py`` (whose own
comment says it mirrors the first), and as a POSIX-only bare ``import fcntl`` in
``local_approval_access.py``. The chained loop ledger needed a fourth copy and got
this module instead.

Duplicating a lock implementation per writer is the same defect class as duplicating
a prompt builder per runner: the copies drift, and the drift is invisible until
someone runs on the platform that only one copy handled. The platform branch lives
here once.

Callers keep their own symlink refusal and their own error wrapping, because the
message an operator reads should name the file they were trying to protect --- this
module does not know whether it is guarding an event stream, a ledger, or an approval
record.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import importlib
import os
from pathlib import Path
from typing import Any, BinaryIO

from bounded_loops.domain.errors import EvidenceError

_fcntl: Any | None = None
_msvcrt: Any | None = None
try:  # POSIX: shared/exclusive advisory locks.
    _fcntl = importlib.import_module("fcntl")
except ModuleNotFoundError:  # pragma: no cover - exercised on Windows.
    pass
try:  # Windows: exclusive lock fallback.
    _msvcrt = importlib.import_module("msvcrt")
except ModuleNotFoundError:  # pragma: no cover - exercised on POSIX.
    pass


@contextmanager
def locked_file(lock_path: Path, *, exclusive: bool) -> Iterator[None]:
    """Hold an advisory lock on ``lock_path`` for the duration of the block.

    Raises ``OSError`` if the lock file cannot be opened, which the caller is
    expected to translate into a domain error naming its own subject. Never nest
    two calls on the same path in one process: POSIX ``flock`` is per open file
    description, so the inner release drops the outer lock.
    """
    with lock_path.open("a+b") as handle:
        _acquire(handle, exclusive=exclusive)
        try:
            yield
        finally:
            _release(handle)


def _acquire(handle: BinaryIO, *, exclusive: bool) -> None:
    if _fcntl is not None:
        _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX if exclusive else _fcntl.LOCK_SH)
        return
    if _msvcrt is not None:  # pragma: no cover - exercised on Windows.
        # Windows exposes only an exclusive byte-range primitive here. Correctness
        # of the chain outranks reader concurrency on that platform.
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        _msvcrt.locking(handle.fileno(), _msvcrt.LK_LOCK, 1)
        return
    raise EvidenceError("no supported local file-lock implementation is available")


def _release(handle: BinaryIO) -> None:
    if _fcntl is not None:
        _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
        return
    if _msvcrt is not None:  # pragma: no cover - exercised on Windows.
        handle.seek(0)
        _msvcrt.locking(handle.fileno(), _msvcrt.LK_UNLCK, 1)
