"""Third-party provider packages, discovered through entry points.

The declarative catalog (``provider_catalog.py``) covers every provider that differs only in
data, which is all five shipped ones. This module covers the rest: a provider that needs *code*.
The mechanism is the Packaging-Authority standard one — the same thing pytest, mypy and Sphinx
plugins use — declared by a third-party package as::

    [project.entry-points."bounded_loops.graph.providers"]
    mycloud = "mycloud_bounded_loops:providers"

where ``providers`` is a callable returning ``Mapping[str, CliProfile]``.

This is the only place in the engine that executes code the operator did not write, so it is the
only place with these four rules — and all four are enforced here, not documented and hoped for.

**1. A broken plugin is skipped, never fatal.** An entry point that raises on import or on call
is logged and dropped. A third-party package must not be able to take down a run.

**2. Registration is all-or-nothing per plugin.** Every profile a plugin returns is validated
before any of them is registered. A plugin that offers three providers and one bad one
contributes nothing — half-registering it would leave a graph author with a provider set that
depends on iteration order.

**3. A plugin cannot claim a shipped name.** Refusing this closes the obvious supply-chain move:
a package that registers the name ``claude`` and quietly becomes the thing every existing graph
already binds to. Operator catalogs *may* override shipped names — an operator pointing
``claude`` at their own wrapper is a deliberate local decision, not a package doing it silently.

**4. A plugin cannot forward a credential on its own.** ``env_grant`` names still go through the
operator intersection in ``local_cli_worker._child_env``, so a hostile plugin declaring
``AWS_SECRET_ACCESS_KEY`` gets nothing unless the operator separately allows that exact name.
The plugin declaring it is one key; the operator is the other.

Precedence, tightest authority last: **plugins < shipped < operator catalog.**
"""

from __future__ import annotations

from importlib.metadata import EntryPoint, entry_points
import logging
from typing import Mapping

from bounded_loops.graph.adapters.connectors.local_cli_worker import CLI_PROFILES, CliProfile
from bounded_loops.graph.adapters.connectors.provider_catalog import profile_from_mapping
from bounded_loops.graph.domain.errors import GraphValidationError

_LOGGER = logging.getLogger(__name__)

#: The entry-point group third-party provider packages declare.
PROVIDER_ENTRY_POINT_GROUP = "bounded_loops.graph.providers"


def reconstructed(profiles: Mapping[str, CliProfile]) -> dict[str, CliProfile]:
    """Fresh ``CliProfile`` objects with the same values — never the caller's objects.

    ``dict(shipped)`` was not enough. ``@dataclass(frozen=True)`` and ``MappingProxyType`` both stop
    ordinary assignment, but ``object.__setattr__(CLI_PROFILES["claude"], "binary", "stolen")``
    walks straight past them, and a shallow copy holds the SAME objects — so poisoning the shipped
    profile poisoned the snapshot too. Rebuilding the values means plugin code has nothing shared
    left to reach.

    **This is a boundary, not a sandbox.** A provider plugin is arbitrary code in this process: it
    can monkey-patch the worker itself if it wants to. What this guarantees is narrower and worth
    stating exactly — the engine's own resolution path will not hand a plugin's values to a
    subprocess, and the checks in this module cannot be defeated by mutating something they read.
    """
    return {
        name: CliProfile(
            binary=profile.binary,
            args=tuple(profile.args),
            prompt_via=profile.prompt_via,
            unset_env=tuple(profile.unset_env),
            set_env=dict(profile.set_env),
            env_grant=tuple(profile.env_grant),
            usage_args=tuple(profile.usage_args),
            envelope=profile.envelope,
        )
        for name, profile in profiles.items()
    }


def _as_catalog_mapping(profile: CliProfile) -> dict[str, object]:
    """Round-trip a plugin's ``CliProfile`` back through the catalog validator.

    A plugin hands over a constructed object, so it has already bypassed the TOML schema. Putting
    it back through the same validator is what makes "a plugin cannot smuggle a credential" true
    rather than merely intended: ``set_env`` is dropped here, so a plugin's set_env never reaches
    a subprocess at all, and every ``env_grant`` entry must still look like a NAME.
    """
    return {
        "binary": profile.binary,
        "args": list(profile.args),
        "prompt_via": profile.prompt_via,
        "unset_env": list(profile.unset_env),
        "env_grant": list(profile.env_grant),
        "usage_args": list(profile.usage_args),
        "envelope": profile.envelope,
    }


def _validated(name: str, profile: object, *, source: str) -> CliProfile:
    if not isinstance(profile, CliProfile):
        raise GraphValidationError(
            "provider_plugin", f"/{name}",
            f"{source} returned {type(profile).__name__} for {name!r}, not a CliProfile",
        )
    if profile.set_env:
        raise GraphValidationError(
            "provider_plugin", f"/{name}/set_env",
            f"{source} sets environment VALUES for {name!r}; a plugin may request names, never "
            "supply values",
        )
    return profile_from_mapping(name, _as_catalog_mapping(profile), pointer=f"/{name}")


def _load_one(entry: EntryPoint, *, shipped: Mapping[str, CliProfile]) -> Mapping[str, CliProfile]:
    """Load and fully validate one plugin, or return nothing at all."""
    factory = entry.load()
    offered = factory()
    if not isinstance(offered, Mapping):
        raise GraphValidationError(
            "provider_plugin", f"/{entry.name}",
            f"entry point {entry.name!r} returned {type(offered).__name__}, not a mapping of "
            "provider name to CliProfile",
        )
    source = f"provider plugin {entry.name!r}"
    accepted: dict[str, CliProfile] = {}
    for provider_name, profile in offered.items():
        if not isinstance(provider_name, str) or not provider_name:
            raise GraphValidationError(
                "provider_plugin", f"/{entry.name}", f"{source} used a non-string provider name",
            )
        if provider_name in shipped:
            raise GraphValidationError(
                "provider_plugin", f"/{provider_name}",
                f"{source} tries to redefine the shipped provider {provider_name!r}. A package "
                "may not silently become the provider existing graphs already bind to; if you "
                "meant to redirect it, do so explicitly in your own provider catalog.",
            )
        accepted[provider_name] = _validated(provider_name, profile, source=source)
    return accepted


def load_provider_plugins(
    *,
    shipped: Mapping[str, CliProfile] = CLI_PROFILES,
    group: str = PROVIDER_ENTRY_POINT_GROUP,
) -> Mapping[str, CliProfile]:
    """Every provider offered by installed third-party packages, fail-safe.

    Never raises. A plugin that raises on import, returns the wrong shape, claims a shipped name,
    or offers one bad profile is logged at WARNING and contributes nothing.
    """
    # Snapshotted BEFORE any plugin code runs, and every check below reads the snapshot.
    #
    # The P3 audit broke the first version of this with two lines: a factory that did
    # ``CLI_PROFILES["claude"] = CliProfile("stolen-binary", set_env={"AWS_SECRET_ACCESS_KEY": …})``
    # and then returned a harmless mapping. Every guard here inspected only the RETURNED mapping, so
    # the shipped profile was replaced and a credential VALUE reached the subprocess with no operator
    # grant anywhere in the path. Registration-by-name was a sticker; mutating the shared object was
    # the door. ``CLI_PROFILES`` is now a ``MappingProxyType`` as well — belt and braces, because a
    # plugin can also reach any other mutable module global it can import.
    baseline = reconstructed(shipped)
    discovered: dict[str, CliProfile] = {}
    for entry in entry_points(group=group):
        try:
            accepted = _load_one(entry, shipped=baseline)
        except GraphValidationError as refused:
            _LOGGER.warning(
                "provider plugin %r refused: [%s] %s — %s",
                entry.name, refused.code, refused.pointer, refused.message,
            )
            continue
        except KeyboardInterrupt:
            # The operator's Ctrl-C, not the plugin's to swallow.
            raise
        except BaseException as broken:  # noqa: BLE001 — a third-party package must not kill the run
            # ``Exception`` alone let a plugin calling ``sys.exit()`` take the process down, which
            # made "a broken plugin is skipped, never fatal" false for the easiest possible mistake.
            _LOGGER.warning(
                "provider plugin %r could not be loaded (%s): %s",
                entry.name, type(broken).__name__, broken,
            )
            continue
        collision = sorted(set(accepted) & set(discovered))
        if collision:
            _LOGGER.warning(
                "provider plugin %r offers provider name(s) %s already offered by another "
                "plugin; skipping this plugin entirely rather than letting load order decide",
                entry.name, ", ".join(collision),
            )
            continue
        discovered.update(accepted)
    return discovered
