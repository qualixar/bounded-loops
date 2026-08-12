"""Run-store helpers for resumable bounded-loop runs."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import time
from pathlib import Path

from bounded_loops.domain.errors import ManifestError
from bounded_loops.domain.models import Outcome

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def validate_run_id(run_id: str) -> str:
    if not _RUN_ID_RE.fullmatch(run_id):
        raise ManifestError(
            "run_id must be 1-128 chars: letters, digits, '.', '_', '-' only; "
            "must start with a letter or digit"
        )
    return run_id


def run_dir(loop_dir: Path, run_id: str, *, storage_root: Path | None = None) -> Path:
    safe = validate_run_id(run_id)
    return _runs_root(loop_dir, storage_root) / safe


def run_workspace(loop_dir: Path, run_id: str, *, storage_root: Path | None = None) -> Path:
    return run_dir(loop_dir, run_id, storage_root=storage_root) / "workspace"


def run_ledger(loop_dir: Path, run_id: str, *, storage_root: Path | None = None) -> Path:
    return run_dir(loop_dir, run_id, storage_root=storage_root) / "ledger.jsonl"


def run_db(loop_dir: Path, *, storage_root: Path | None = None) -> Path:
    return _runs_root(loop_dir, storage_root).parent / "runs.sqlite"


def _runs_root(loop_dir: Path, storage_root: Path | None) -> Path:
    package_root = loop_dir.resolve()
    if storage_root is None:
        return package_root / ".bounded-loops" / "runs"
    root = storage_root.resolve()
    if root == package_root or root.is_relative_to(package_root):
        raise ManifestError("controller storage root must be outside the loop package")
    return root / "runs"


def begin_run(
    *,
    loop_dir: Path,
    run_id: str,
    workspace: Path,
    ledger_path: Path,
    storage_root: Path | None = None,
) -> Path:
    """Persist a run record before any runner or gate can execute.

    The operation is idempotent for an existing run, which lets a controller
    resume after a crash without rewriting its original creation evidence.
    """
    directory = run_dir(loop_dir, run_id, storage_root=storage_root)
    directory.mkdir(parents=True, exist_ok=True)
    metadata_path = directory / "metadata.json"
    if metadata_path.exists():
        return metadata_path
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.touch(exist_ok=True)
    metadata = {
        "run_id": run_id,
        "loop_dir": str(loop_dir.resolve()),
        "workspace": str(workspace.resolve()),
        "ledger_path": str(ledger_path.resolve()),
        "status": "STARTING",
        "reason": "controller-created-before-execution",
        "laps": 0,
    }
    _write_json_atomically(metadata_path, metadata)
    _upsert_run_values(
        db_path=run_db(loop_dir, storage_root=storage_root),
        run_id=run_id,
        loop_dir=loop_dir,
        workspace=workspace,
        ledger_path=ledger_path,
        status="STARTING",
        reason="controller-created-before-execution",
        laps=0,
    )
    return metadata_path


def write_run_metadata(
    *,
    loop_dir: Path,
    run_id: str,
    outcome: Outcome,
    workspace: Path,
    storage_root: Path | None = None,
) -> Path:
    directory = run_dir(loop_dir, run_id, storage_root=storage_root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "metadata.json"
    _write_json_atomically(
        path,
        {
            "run_id": run_id,
            "loop_dir": str(loop_dir.resolve()),
            "workspace": str(workspace.resolve()),
            "ledger_path": str(outcome.ledger_path),
            "status": outcome.status.value,
            "reason": outcome.reason,
            "laps": outcome.laps,
        },
    )
    _upsert_run_values(
        db_path=run_db(loop_dir, storage_root=storage_root),
        run_id=run_id,
        loop_dir=loop_dir,
        workspace=workspace,
        ledger_path=outcome.ledger_path,
        status=outcome.status.value,
        reason=outcome.reason,
        laps=outcome.laps,
    )
    return path


def list_runs(loop_dir: Path, *, storage_root: Path | None = None) -> list[dict]:
    db_path = run_db(loop_dir, storage_root=storage_root)
    if db_path.is_file():
        try:
            return _list_runs_from_db(db_path)
        except sqlite3.Error:
            pass
    base = _runs_root(loop_dir, storage_root)
    if not base.is_dir():
        return []
    results: list[dict] = []
    for metadata_path in sorted(base.glob("*/metadata.json")):
        try:
            data = json.loads(metadata_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                results.append(data)
        except (OSError, json.JSONDecodeError):
            results.append({"run_id": metadata_path.parent.name, "error": "metadata unreadable"})
    return results


def read_run_receipt(
    loop_dir: Path,
    run_id: str,
    *,
    storage_root: Path | None = None,
) -> dict:
    """Read one persisted run without allowing run-id or symlink escapes."""
    directory = run_dir(loop_dir, run_id, storage_root=storage_root)
    runs_root = _runs_root(loop_dir, storage_root).resolve()
    resolved_directory = directory.resolve()
    if not resolved_directory.is_relative_to(runs_root):
        raise ManifestError("run directory resolves outside .bounded-loops/runs")
    if not resolved_directory.is_dir():
        raise ManifestError(f"run '{run_id}' does not exist")

    metadata_path = resolved_directory / "metadata.json"
    ledger_path = resolved_directory / "ledger.jsonl"
    if metadata_path.is_symlink() or ledger_path.is_symlink():
        raise ManifestError("run receipt files must not be symlinks")

    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ManifestError(f"run '{run_id}' metadata is missing") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"run '{run_id}' metadata is unreadable") from exc
    if not isinstance(metadata, dict):
        raise ManifestError(f"run '{run_id}' metadata must be a JSON object")

    try:
        raw_lines = ledger_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise ManifestError(f"run '{run_id}' ledger is missing") from exc
    except OSError as exc:
        raise ManifestError(f"run '{run_id}' ledger is unreadable") from exc

    entries: list[dict] = []
    for line_number, line in enumerate(raw_lines, 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ManifestError(
                f"run '{run_id}' ledger has invalid JSON on line {line_number}"
            ) from exc
        if not isinstance(entry, dict):
            raise ManifestError(
                f"run '{run_id}' ledger line {line_number} must be a JSON object"
            )
        entries.append(entry)
    return {"metadata": metadata, "entries": entries}


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            loop_dir TEXT NOT NULL,
            workspace TEXT NOT NULL,
            ledger_path TEXT NOT NULL,
            status TEXT NOT NULL,
            reason TEXT NOT NULL,
            laps INTEGER NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    return conn


def _write_json_atomically(path: Path, value: dict) -> None:
    """Durably replace one controller-owned JSON record without a torn file."""
    fd, temp_name = tempfile.mkstemp(prefix=".metadata-", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _upsert_run_values(
    *,
    db_path: Path,
    run_id: str,
    loop_dir: Path,
    workspace: Path,
    ledger_path: Path,
    status: str,
    reason: str,
    laps: int,
) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO runs (
                run_id, loop_dir, workspace, ledger_path, status, reason, laps, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                loop_dir=excluded.loop_dir,
                workspace=excluded.workspace,
                ledger_path=excluded.ledger_path,
                status=excluded.status,
                reason=excluded.reason,
                laps=excluded.laps,
                updated_at=excluded.updated_at
            """,
            (
                run_id,
                str(loop_dir.resolve()),
                str(workspace.resolve()),
                str(ledger_path.resolve()),
                status,
                reason,
                laps,
                time.time(),
            ),
        )


def _list_runs_from_db(db_path: Path) -> list[dict]:
    with _connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT run_id, loop_dir, workspace, ledger_path, status, reason, laps, updated_at
            FROM runs
            ORDER BY updated_at ASC, run_id ASC
            """
        ).fetchall()
    return [dict(row) for row in rows]
