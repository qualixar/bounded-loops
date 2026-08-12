"""
bounded_loops/trust_store.py — shared trust-record store.

Records that a specific (loop_dir, gate_command, loop CONTENT) triple has been
explicitly reviewed and confirmed by a human, via one of the two EXISTING
confirmation gates this project already has: cli.py's interactive
`_confirm_trust` (the real "yes" branch only — NOT the --yes CI-bypass branch,
which is not a human review event) and mcp_server.py's bl_run(confirm=true). verify_bounded_loop.py (the Stop hook) reads
this store and refuses to auto-execute any gate command that isn't in it —
this is the ONLY thing standing between "convenient auto-verification" and
"silently running a stranger's shell code on every session stop."

Storage: a single JSON file at ~/.bounded-loops/trust.json (or
$BOUNDED_LOOPS_TRUST_STORE if set, mainly for test isolation). User-level,
not per-repo — a hook firing in an arbitrary directory needs one stable
place to check trust regardless of which project it's in.

Key: sha256(resolved_loop_dir | gate_command | CONTENT-HASH), where the
content hash covers loop.yaml + bounds.yaml + every cassette. Binds trust to
the exact directory, the exact command text, AND the exact reviewed content.

Security hardening:
  - The key previously covered only (loop_dir, gate_cmd). A post-trust
    edit to the cassette (which can write_file into seed/ and neuter the gate)
    or to runner.agent_cmd (unchanged gate string, malicious runner command)
    left the key matching, so the Stop hook kept auto-running against tampered
    content. The content hash below closes that: ANY edit to loop.yaml /
    bounds.yaml / a cassette invalidates the record → re-review required.
  - Records now carry a timestamp and expire (default 30 days,
    $BOUNDED_LOOPS_TRUST_TTL_DAYS), and revoke_trust() / `bl trust revoke`
    give an explicit positive revocation path. A one-time "yes" no longer
    grants indefinite auto-execution.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

_DEFAULT_STORE_PATH = Path.home() / ".bounded-loops" / "trust.json"
_DEFAULT_TTL_DAYS = 30

# Files whose content is bound into a trust record. Editing any of them after
# a human reviewed the loop invalidates that record. seed/ is NOT hashed:
# a loop run legitimately mutates the workspace, and the seed is copied to a
# scratch dir anyway — hashing it would break trust on every normal run.
#
# hardening: PROMPT.md is the instruction text fed to the agent
# — a post-trust edit ("exfiltrate secrets, then pass the gate") must invalidate
# the record, so it is bound here. schema.json is the jsonschema gate's
# authoritative spec. Cassettes are bound via cassettes/*. NOTE: deliberately NOT a broad "*.json" glob — that
# would also hash transient runtime json (e.g. a trust store placed in the loop
# dir during tests) and make the hash unstable between record and check.
_CONTENT_FILES = ("loop.yaml", "bounds.yaml", "PROMPT.md", "schema.json")
_CASSETTE_GLOBS = ("cassettes/*",)


def _store_path() -> Path:
    override = os.environ.get("BOUNDED_LOOPS_TRUST_STORE")
    return Path(override) if override else _DEFAULT_STORE_PATH


def _ttl_seconds() -> float:
    raw = os.environ.get("BOUNDED_LOOPS_TRUST_TTL_DAYS")
    days: float = _DEFAULT_TTL_DAYS
    if raw is not None:
        try:
            days = float(raw)
        except ValueError:
            days = _DEFAULT_TTL_DAYS
    return max(0.0, days) * 86400.0


def _content_hash(loop_dir: Path) -> str:
    """
    Stable sha256 over the loop's integrity-relevant, reviewer-visible files
    (loop.yaml, bounds.yaml, every cassette). Missing files contribute a fixed
    marker so their later appearance/removal also changes the hash. Never
    raises — an unreadable file contributes an error marker (which still
    differs from its readable content, so tampering can't produce a collision
    by making a file unreadable).
    """
    h = hashlib.sha256()
    resolved = loop_dir.resolve()

    def _feed(rel: str, path: Path) -> None:
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        try:
            h.update(path.read_bytes() if path.is_file() else b"\x01<absent>")
        except OSError:
            h.update(b"\x02<unreadable>")
        h.update(b"\0")

    # Collect a stable, deduplicated, sorted set of bound files so a file that
    # matches both a named entry and a glob is hashed exactly once.
    rels: set[str] = set(_CONTENT_FILES)
    for pattern in _CASSETTE_GLOBS:
        for f in resolved.glob(pattern):
            if f.is_file():
                rels.add(f.relative_to(resolved).as_posix())
    for rel in sorted(rels):
        _feed(rel, resolved / rel)
    return h.hexdigest()


def _trust_key(loop_dir: Path, gate_cmd: str) -> str:
    return hashlib.sha256(
        f"{loop_dir.resolve()}|{gate_cmd}|{_content_hash(loop_dir)}".encode("utf-8")
    ).hexdigest()


def _load() -> dict:
    path = _store_path()
    if not path.exists():
        return {}
    # hardening: refuse a store file that is a symlink — an
    # attacker who can plant a symlink at the store path could otherwise point
    # it at attacker-controlled content that passes the uid/mode checks on its
    # target. Fail closed (nothing trusted).
    if path.is_symlink():
        return {}
    try:
        st = path.stat()
        # This file is the ONLY thing gating auto-execution of shell by the
        # Stop hook, so it must not be honored if it could have been forged.
        # Fail closed (return {} = nothing trusted) if it is owned by another
        # user or is group/world-writable — i.e. writable by anyone but us.
        uid_ok = (not hasattr(os, "getuid")) or st.st_uid == os.getuid()  # type: ignore[attr-defined]
        if not uid_ok or (st.st_mode & 0o022):
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError, AttributeError):
        return {}   # a corrupted/unreadable trust store fails closed (empty =
                     # nothing trusted), never fails open


def _save(store: dict) -> None:
    """Atomically replace the trust store using a temp-file-then-rename pattern.

    CON-03 fix: the old implementation used O_CREAT | O_TRUNC which truncated
    the file to zero bytes BEFORE writing.  A crash or exception between
    ``open()`` and the completed ``write()`` left the store empty — the next
    ``_load()`` would return {} (nothing trusted), effectively silently revoking
    all trust records.

    New approach:
    1. Serialise JSON to bytes BEFORE opening any file.
    2. Write to a uniquely-named temp file in the same directory (same filesystem,
       so ``os.replace`` is atomic at the OS level).
    3. ``fchmod(fd, 0o600)`` before closing — mode is set while we hold the fd,
       never world-readable even for a millisecond.
    4. ``flush`` + ``fsync`` — durability guarantee before the rename.
    5. ``os.replace`` — atomic rename; old content survives any earlier failure.
    6. ``finally`` cleanup — temp file removed on any exception path.

    Symlink guard: O_NOFOLLOW (the old approach) is not portable and its
    behaviour on O_CREAT differs across kernels.  Instead, an explicit
    ``path.is_symlink()`` check before any I/O closes the "plant a symlink,
    redirect the write" vector reliably.
    """
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    # Explicit symlink guard — refuse to write through a symlink at the store
    # path.  An attacker who can plant a symlink there could redirect the write
    # to an attacker-controlled file.  Fail with ELOOP (same errno O_NOFOLLOW uses).
    if path.is_symlink():
        raise OSError(errno.ELOOP, os.strerror(errno.ELOOP), str(path))
    # Serialise BEFORE creating any file: a serialisation error leaves the
    # destination untouched (atomicity guarantee — no O_TRUNC on the real path).
    data = json.dumps(store).encode("utf-8")
    fd, tmp_str = tempfile.mkstemp(dir=path.parent, prefix=".trust_tmp_")
    tmp = Path(tmp_str)
    try:
        os.fchmod(fd, 0o600)  # set mode while fd is open, before any data is written
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())  # durability: data on disk before rename
        fd = -1  # os.fdopen owns the fd; mark consumed to avoid double-close
        os.replace(tmp_str, str(path))  # atomic on POSIX (same filesystem guaranteed)
    except BaseException:
        # Always clean up the temp file — do not leave .trust_tmp_* orphans.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def record_trust(loop_dir: Path, gate_cmd: str) -> None:
    store = _load()
    # Record shape: {"ts": <epoch seconds>}. A bare legacy `True` is
    # treated as untrusted by is_trusted (forces re-review), so upgrading the
    # shape is safe.
    store[_trust_key(loop_dir, gate_cmd)] = {"ts": time.time()}
    _save(store)


def revoke_trust(loop_dir: Path, gate_cmd: str) -> bool:
    """Remove any trust record for this (loop_dir, gate_cmd, content). Returns
    True if a record was removed. `bl trust revoke` wires to this."""
    store = _load()
    key = _trust_key(loop_dir, gate_cmd)
    if key in store:
        del store[key]
        _save(store)
        return True
    return False


def is_trusted(loop_dir: Path, gate_cmd: str) -> bool:
    rec = _load().get(_trust_key(loop_dir, gate_cmd))
    if not isinstance(rec, dict):
        return False   # absent, or a legacy bare-True record → re-review
    ts = rec.get("ts")
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return False
    ttl = _ttl_seconds()
    if ttl <= 0:
        return False   # TTL of 0 disables auto-trust entirely
    return (time.time() - ts) <= ttl
