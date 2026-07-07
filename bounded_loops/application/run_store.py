"""Run-store helpers for resumable bounded-loop runs."""

from __future__ import annotations

import json
import re
import sqlite3
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


def run_dir(loop_dir: Path, run_id: str) -> Path:
    safe = validate_run_id(run_id)
    return loop_dir.resolve() / ".bounded-loops" / "runs" / safe


def run_workspace(loop_dir: Path, run_id: str) -> Path:
    return run_dir(loop_dir, run_id) / "workspace"


def run_ledger(loop_dir: Path, run_id: str) -> Path:
    return run_dir(loop_dir, run_id) / "ledger.jsonl"


def run_db(loop_dir: Path) -> Path:
    return loop_dir.resolve() / ".bounded-loops" / "runs.sqlite"


def write_run_metadata(
    *,
    loop_dir: Path,
    run_id: str,
    outcome: Outcome,
    workspace: Path,
) -> Path:
    directory = run_dir(loop_dir, run_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "metadata.json"
    path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "loop_dir": str(loop_dir.resolve()),
                "workspace": str(workspace.resolve()),
                "ledger_path": str(outcome.ledger_path),
                "status": outcome.status.value,
                "reason": outcome.reason,
                "laps": outcome.laps,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    _upsert_run_db(
        db_path=run_db(loop_dir),
        run_id=run_id,
        loop_dir=loop_dir,
        workspace=workspace,
        outcome=outcome,
    )
    return path


def list_runs(loop_dir: Path) -> list[dict]:
    db_path = run_db(loop_dir)
    if db_path.is_file():
        try:
            return _list_runs_from_db(db_path)
        except sqlite3.Error:
            pass
    base = loop_dir.resolve() / ".bounded-loops" / "runs"
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


def _upsert_run_db(
    *,
    db_path: Path,
    run_id: str,
    loop_dir: Path,
    workspace: Path,
    outcome: Outcome,
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
                str(outcome.ledger_path),
                outcome.status.value,
                outcome.reason,
                outcome.laps,
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