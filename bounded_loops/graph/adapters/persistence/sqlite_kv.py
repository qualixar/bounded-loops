"""A durable exact key/value store over SQLite — the concrete DurableKeyValuePort the
memory spine persists through.

SLM V4 is a SEMANTIC memory (remember/recall by meaning), NOT an exact key/value store
with prefix listing: `remember` extracts facts (it does not store a value verbatim),
`recall` is a fuzzy ranked limit-N query with no exact-key lookup, `cache` is a TTL
cache, and `list` is "recent N" with no prefix. So SLM cannot honestly satisfy
DurableKeyValuePort's exact contract. This SQLite-backed store does — verbatim
get/set/delete and CORRECT prefix listing — at the same durability tier (SLM is itself
SQLite-backed). Semantic recall over SLM is a SEPARATE capability (a SemanticMemoryPort),
never this exact-KV port.
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3

from bounded_loops.graph.domain.errors import GraphIntegrityError


class SqliteDurableKeyValue:
    """A ``DurableKeyValuePort`` backed by a single SQLite file (WAL, durable across
    processes). Keys and values are opaque strings the memory store produces and
    interprets; this adapter never parses them."""

    def __init__(self, path: Path) -> None:
        if path.is_symlink():
            raise GraphIntegrityError("memory store path must not be a symlink")
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.execute("PRAGMA journal_mode=WAL")  # durable writes + concurrent readers
        return conn

    def set(self, key: str, value: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT INTO kv(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            conn.commit()

    def get(self, key: str) -> str | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def delete(self, key: str) -> bool:
        with closing(self._connect()) as conn:
            cursor = conn.execute("DELETE FROM kv WHERE key = ?", (key,))
            conn.commit()
            return cursor.rowcount > 0

    def list_prefix(self, prefix: str) -> tuple[tuple[str, str], ...]:
        # substr(key, 1, N) == prefix is an EXACT character-prefix match. LIKE
        # prefix||'%' would be wrong here: a netstring key can contain '_' or '%'
        # (namespace parts are arbitrary strings), and LIKE would treat those as
        # wildcards. substr matches literally, so a crafted namespace cannot widen it.
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT key, value FROM kv WHERE substr(key, 1, ?) = ?",
                (len(prefix), prefix),
            ).fetchall()
        return tuple((str(row[0]), str(row[1])) for row in rows)
