"""Path resolution, allowlist canonicalization, and the secure egress-config write.

This module is a pure, obedient PRODUCER of the config contract documented in
``egress_posture.py`` (module docstring there is authoritative) — it never
modifies that reader, and every function here that writes or claims to
validate config content is proven, in-process (``verify_round_trip``), to
agree with what ``resolve_egress_posture()`` will actually accept.

Security posture, mirroring ``trust_store.py::_save`` (the only other
installer-style, security-relevant, user-home config writer in this project):
parent directory ``0700``, file ``0600`` created with the mode from the start
(never write-then-chmod, which leaves a race window), and ``O_NOFOLLOW`` at
the exact syscall that opens the file so a symlink planted at the final path
component is refused atomically, never followed and never silently replaced.
"""

from __future__ import annotations

from dataclasses import dataclass
import errno
import json
import os
from pathlib import Path
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
    skip), but leaving it out keeps the written file minimal and unambiguous."""
    if posture is not EgressPosture.ALLOWLIST:
        return {"posture": posture.value}
    return {"posture": posture.value, "allowlist": list(allowlist)}


def write_config_atomically(path: Path, payload: Mapping[str, object]) -> None:
    """Write *payload* as JSON at *path*: parent dir ``0700``, file ``0600``
    created with that mode from the start, ``O_NOFOLLOW`` so a symlink at the
    final path component is refused rather than followed or replaced."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise GraphInitError(f"could not create '{path.parent}': {exc}") from exc
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass  # best-effort, mirrors trust_store._save — not fatal on odd filesystems

    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise GraphInitError(f"'{path}' is a symlink; refusing to write through it") from exc
        raise GraphInitError(f"could not write '{path}': {exc}") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    except OSError as exc:
        raise GraphInitError(f"could not write '{path}': {exc}") from exc


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
