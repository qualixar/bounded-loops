"""Path resolution, allowlist canonicalization, and the secure egress-config write.

This module is a pure, obedient PRODUCER of the config contract documented in
``egress_posture.py`` (module docstring there is authoritative) — it never
modifies that reader, and every function here that writes or claims to
validate config content is proven, in-process (``verify_round_trip``), to
agree with what ``resolve_egress_posture()`` will actually accept.

Security posture — ATOMIC secure write (fix for a live-proven MAJOR, see
``write_config_atomically``'s docstring): a fresh, uniquely-named temp file is
created in the SAME directory as the target (0600 forced via ``fchmod``,
never relying on the create-time ``mode`` argument alone), fsynced, verified
through the SAME fail-closed reader `bl graph run` uses, and only THEN
``os.replace()``d into place. This differs from — and improves on —
``trust_store.py::_save``'s simpler in-place ``O_CREAT|O_TRUNC`` pattern
specifically because ``trust_store.py`` never overwrites an EXISTING trust
record file with a different mode in practice; this module's target is a
user-facing, hand-editable config file that a previous run (or the user) may
have left in an unexpected mode, so mode must be forced on every write, not
just first creation. On any failure the temp file is removed and the target
is left byte-for-byte and mode-for-mode untouched — a bad payload never
reaches the live config.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import secrets
from typing import Mapping, Sequence

from bounded_loops.graph.adapters.enforcement.egress_posture import (
    EgressPosture,
    EgressPostureConfig,
    resolve_egress_posture,
)
from bounded_loops.graph.application.egress_broker import split_destination
from bounded_loops.graph.application.execution_policy import NetworkDestination
from bounded_loops.graph.domain.errors import GraphValidationError
from bounded_loops.graph.init.errors import GraphInitError

# Mirrors egress_posture.py's own private _DEFAULT_ALLOWLIST_PORT. Duplicated (not
# imported — that constant is private) rather than re-derived; pinned equal to it by
# test_default_allowlist_port_constant_matches_egress_posture_modules_own_default.
_DEFAULT_ALLOWLIST_PORT = 443
_ENV_CONFIG_PATH = "BOUNDED_LOOPS_EGRESS_CONFIG"


# ── path resolution ──────────────────────────────────────────────────────────────


def default_config_path() -> Path:
    """The path `bl graph run` resolves to when nothing overrides it.

    Deliberately a FUNCTION, not a module-level constant: ``egress_posture.py``'s
    own ``_DEFAULT_CONFIG_PATH`` is computed once at import time, which is exactly
    right for that module (every one of its own tests overrides the path
    explicitly). This installer's tests instead rely on the autouse HOME-redirect
    fixture in ``tests/conftest.py``, which only takes effect if ``Path.home()``
    is re-evaluated at CALL time — so this stays a function, not a frozen value.
    """
    return Path.home() / ".bounded-loops" / "egress.json"


def resolve_config_path(cli_arg: str | None, environ: Mapping[str, str]) -> Path:
    """Precedence: explicit ``--config`` CLI arg > ``BOUNDED_LOOPS_EGRESS_CONFIG``
    env var > the default path — mirrors ``egress_posture._config_path``'s own
    env-then-default precedence, with the CLI flag added as a new highest tier
    (matching this project's universal "explicit argument beats env var" rule)."""
    if cli_arg:
        return Path(cli_arg)
    env_override = environ.get(_ENV_CONFIG_PATH)
    if env_override:
        return Path(env_override)
    return default_config_path()


# ── allowlist entry canonicalization (reuses the reader's own public parsers) ──


def canonicalize_allowlist_entry(text: str) -> str:
    """Validate one ``host`` / ``host:port`` entry and return its canonical form.

    Reuses ``split_destination`` and ``NetworkDestination`` — the SAME public
    primitives ``egress_posture._parse_allowlist_entries`` itself calls — so this
    pre-flight check can never disagree with what the fail-closed reader accepts
    later. Raises ``GraphInitError`` (never a raw ``ValueError``/``GraphValidationError``)
    with a message safe to print directly to the CLI.
    """
    try:
        host, port = split_destination(text)
    except ValueError as exc:
        raise GraphInitError(f"invalid allowlist entry {text!r}: {exc}") from exc
    resolved_port = port if port is not None else _DEFAULT_ALLOWLIST_PORT
    try:
        NetworkDestination(hostname=host, port=resolved_port)
    except GraphValidationError as exc:
        raise GraphInitError(f"invalid allowlist entry {text!r}: {exc.message}") from exc
    canonical_host = host.strip().lower()
    if resolved_port == _DEFAULT_ALLOWLIST_PORT:
        return canonical_host
    return f"{canonical_host}:{resolved_port}"


def canonicalize_allowlist_entries(entries: Sequence[str]) -> tuple[str, ...]:
    """Validate every entry, then de-duplicate by normalized (host, port) identity
    while preserving first-seen order.

    De-duplication matters: writing two entries that normalize to the SAME
    ``NetworkDestination`` (e.g. ``"API.Example.COM"`` and ``"api.example.com:443"``)
    would otherwise round-trip into a file the reader's OWN uniqueness invariant
    (``EgressPostureConfig.__post_init__``) rejects on read-back — precisely the
    class of "installer writes a file the fail-closed reader would REJECT" bug
    this package must never ship.
    """
    seen: dict[str, None] = {}
    for entry in entries:
        canonical = canonicalize_allowlist_entry(entry)
        seen.setdefault(canonical, None)
    return tuple(seen)


def flatten_allowlist_flag(values: Sequence[str]) -> tuple[str, ...]:
    """Flatten repeated ``--allowlist`` flag values, each optionally itself
    comma-separated (mirrors ``BOUNDED_LOOPS_EGRESS_ALLOWLIST``'s own comma-separated
    convention), stripping whitespace and skipping blank entries from stray commas."""
    flattened: list[str] = []
    for value in values:
        flattened.extend(part.strip() for part in value.split(",") if part.strip())
    return tuple(flattened)


# ── config payload + secure write ────────────────────────────────────────────────


def build_config_payload(posture: EgressPosture, allowlist: Sequence[str] = ()) -> dict[str, object]:
    """Build the exact ``{"posture": ..., "allowlist": [...]}`` shape the reader
    accepts. The ``allowlist`` key is OMITTED entirely outside ALLOWLIST posture —
    the reader never inspects it there anyway (see egress_posture.py's precedence
    skip), but leaving it out keeps the written file minimal and unambiguous.

    Canonicalizes + de-dupes *allowlist* itself (m3, defense-in-depth): a caller
    of this MODULE's public API — not only ``cli_init.py``'s own flow, which
    already canonicalizes before calling this — must never be able to emit a
    uniqueness-violating allowlist the reader's own invariant would reject."""
    if posture is not EgressPosture.ALLOWLIST:
        return {"posture": posture.value}
    return {"posture": posture.value, "allowlist": list(canonicalize_allowlist_entries(allowlist))}


def _unique_temp_path(path: Path) -> Path:
    """A temp path in the SAME directory as *path* — ``os.replace`` requires the
    source and destination to be on the same filesystem — with an unpredictable
    suffix (pid + 16 hex chars of ``secrets.token_hex``), never a fixed name a
    local attacker could pre-plant a symlink at ahead of time."""
    return path.parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"


def write_config_atomically(path: Path, payload: Mapping[str, object]) -> EgressPostureConfig:
    """Write *payload* as the egress config at *path*, ATOMICALLY and SECURELY,
    returning the verified ``EgressPostureConfig`` that now lives there.

    Fixes a live-proven MAJOR: POSIX only applies the ``mode`` argument of
    ``os.open()`` at file CREATION. The previous implementation opened *path*
    directly with ``O_CREAT|O_TRUNC``, which truncates an EXISTING inode's
    CONTENT but leaves that inode's OLD mode untouched — pre-creating the file
    at ``0o666`` and then "overwriting" it via the installer left it ``0o666``
    afterward too (content updated, mode silently unchanged). The fix below
    never truncates the target's own inode at all:

    1. Refuse a symlink AT *path* up front — a fast, friendly refusal for the
       common case (never silently replaced). This check is a UX nicety, not
       the security boundary: see step 5's note for why the real guarantee
       against writing through a symlink holds even if this check is bypassed
       or raced (TOCTOU) — proven directly by
       ``test_write_config_atomically_never_writes_through_a_symlink_even_if_the_precheck_is_bypassed``.
    2. Create the parent dir ``0700`` (best-effort ``chmod``, mirrors
       ``trust_store.py::_save`` — not fatal on an odd filesystem).
    3. Create a FRESH, uniquely-named temp file in the SAME directory as
       *path* with ``O_EXCL|O_NOFOLLOW`` (never an existing or symlinked
       path), then ``os.fchmod`` the open descriptor to force ``0o600`` —
       belt-and-suspenders on top of the create-time mode, independent of
       umask.
    4. Write the JSON, ``flush`` + ``os.fsync`` before the temp file is ever
       considered "done" — its content is durable before it can become live.
    5. Read the TEMP file back through ``verify_round_trip`` — the SAME
       fail-closed reader `bl graph run` uses — BEFORE it ever replaces the
       real config. A bad payload is caught HERE and never reaches *path* at
       all: the previous config (if any) is left completely untouched. THEN
       ``os.replace(tmp, path)`` — atomic; the kernel repoints the directory
       entry to the temp's fresh (0600) inode. ``rename(2)`` (what
       ``os.replace`` calls) never opens or follows a symlink at *path* — if
       *path* is a symlink it replaces the symlink ENTRY itself, so this step
       is symlink-safe regardless of what step 1 observed or when.
    6. On ANY failure at any step after the temp file is created, it is
       unlinked (best-effort) — no litter on success (already consumed by
       ``os.replace``) or on failure.
    """
    if os.path.islink(path):
        raise GraphInitError(f"'{path}' is a symlink; refusing to write through it")

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise GraphInitError(f"could not create '{path.parent}': {exc}") from exc
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass  # best-effort, mirrors trust_store._save — not fatal on odd filesystems

    tmp_path = _unique_temp_path(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(tmp_path, flags, 0o600)
    except OSError as exc:
        raise GraphInitError(f"could not create a temp file in '{path.parent}': {exc}") from exc

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)  # force the mode regardless of umask
            handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

        try:
            verified = verify_round_trip(tmp_path)
        except GraphValidationError as exc:
            raise GraphInitError(
                "internal error — the newly written config failed its own reader before "
                f"being committed; the previous config at '{path}' (if any) was left "
                f"completely untouched: {exc}"
            ) from exc

        os.replace(tmp_path, path)
    except OSError as exc:
        raise GraphInitError(f"could not write '{path}': {exc}") from exc
    finally:
        # On success, os.replace() has already consumed tmp_path — unlink is then
        # a harmless no-op (FileNotFoundError, swallowed). On ANY failure above,
        # this is what guarantees no orphaned temp file is ever left behind.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    return verified


# ── round-trip verification (the non-negotiable proof) ─────────────────────────


def _file_only_environ(path: Path) -> dict[str, str]:
    """An environ containing ONLY the config-path override — deliberately never
    the real ``os.environ`` — so a caller's ambient BOUNDED_LOOPS_EGRESS_POSTURE/
    _ALLOWLIST (exported for an unrelated purpose) can never shadow what this
    check is trying to prove: what the FILE itself says."""
    return {_ENV_CONFIG_PATH: str(path)}


def verify_round_trip(path: Path) -> EgressPostureConfig:
    """Read *path* back through ``egress_posture.resolve_egress_posture`` — the
    exact fail-closed reader `bl graph run` / `LocalGraphRuntimeFacade` consume.
    Raises ``GraphValidationError`` (uncaught here) if the file is somehow invalid;
    callers that just wrote the file should treat that as an internal bug, not a
    user-facing input error."""
    return resolve_egress_posture(environ=_file_only_environ(path))


# ── existing-config detection ────────────────────────────────────────────────────


@dataclass(frozen=True)
class ExistingConfig:
    """A snapshot of whatever already lives at the target config path."""

    path: Path
    is_symlink: bool
    config: EgressPostureConfig | None
    error: str | None


def read_existing_snapshot(path: Path) -> ExistingConfig | None:
    """``None`` iff nothing exists at *path* yet (``os.path.lexists`` — a dangling
    symlink still counts as "existing", never silently treated as "safe to create
    fresh"). Otherwise a symlink is reported without following it; a regular file
    is read back through the SAME fail-closed reader ``verify_round_trip`` uses,
    reporting a corrupt/invalid existing file rather than raising, so the CLI can
    show it to the user before asking whether to overwrite."""
    if not os.path.lexists(path):
        return None
    if os.path.islink(path):
        return ExistingConfig(path=path, is_symlink=True, config=None, error="path is a symlink")
    try:
        config = verify_round_trip(path)
    except GraphValidationError as exc:
        return ExistingConfig(path=path, is_symlink=False, config=None, error=str(exc))
    return ExistingConfig(path=path, is_symlink=False, config=config, error=None)
