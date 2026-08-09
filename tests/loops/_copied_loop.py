"""Helpers for running checked-in loop fixtures without mutating the checkout."""
from __future__ import annotations

from pathlib import Path
import shutil


_RUNTIME_ARTIFACT_PATTERNS = (
    ".bounded-loops",
    ".ledger.jsonl",
    ".STATE.md.runtime",
    "__pycache__",
    ".pytest_cache",
    "*.pyc",
)


def copy_loop(source: Path, destination_root: Path) -> Path:
    """Copy one loop fixture below a caller-owned temporary directory."""

    destination = destination_root / source.name
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns(*_RUNTIME_ARTIFACT_PATTERNS),
    )
    return destination
