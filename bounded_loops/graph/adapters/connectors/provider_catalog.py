"""Declarative provider catalog — add an agent CLI without publishing a Python package.

Five CLI profiles ship hard-coded in ``local_cli_worker.CLI_PROFILES``. Every one of them
differs from the others only in *data*: which binary, which flags, how the prompt arrives,
which JSON envelope carries usage. Requiring a code change (and a release) to add a sixth is
the single biggest thing standing between this engine and "plug-and-play", and it is not a
change that needs code.

So: a TOML file. ``tomllib`` is stdlib on 3.11+, so this adds no dependency.

    [providers.mycli]
    binary = "mycli"
    args = ["--print"]
    prompt_via = "arg"           # or "stdin"
    usage_args = ["--json"]      # how to ask for a machine-readable envelope
    envelope = "claude"          # which shipped parser reads that envelope
    unset_env = ["MYCLI_SESSION"]
    env_grant = ["MYCLI_REGION"] # NAMES this CLI needs forwarded, never values

Two rules make this safe to hand to an operator, and both are enforced here rather than
documented and hoped for:

**A catalog never carries a credential.** ``env_grant`` and ``unset_env`` hold NAMES; a
secret-shaped key in ``set_env`` is refused outright. The engine's job is to decide which
names reach a subprocess — it has never needed to read a value and must not learn how.

**An unknown key is an error, not a shrug.** The schema is closed. A typo'd ``envelop`` that
was silently ignored would leave the operator believing their provider is metered while every
spend cap on it fails closed as unmeasurable — the failure that looks exactly like protection
and is not.
"""

from __future__ import annotations

from pathlib import Path
import tomllib
from typing import Mapping

from bounded_loops.graph.adapters.connectors.cli_envelope import ENVELOPE_PARSERS
from bounded_loops.graph.adapters.connectors.local_cli_worker import CLI_PROFILES, CliProfile
from bounded_loops.graph.domain.errors import GraphValidationError

#: Every key a catalog entry may declare. Mirrors the constructor arguments of ``CliProfile``
#: minus ``set_env`` — see ``_SET_ENV_REFUSED`` below for why that one is not operator-writable.
_ALLOWED_KEYS = frozenset({
    "binary", "args", "prompt_via", "unset_env", "usage_args", "envelope", "env_grant",
})
_LIST_KEYS = frozenset({"args", "unset_env", "usage_args", "env_grant"})
_STRING_KEYS = frozenset({"binary", "prompt_via", "envelope"})

#: Substring match, same word list the authoring validator uses. Catching ``auth_token`` and
#: ``x-api-key`` matters more than the false positive on a variable legitimately named
#: ``TOKEN_BUDGET`` — which an operator can still forward, by name, via ``env_grant``.
_SECRET_WORDS = frozenset({"api_key", "credential", "password", "secret", "token"})

_SET_ENV_REFUSED = (
    "set_env is not settable from a catalog: it holds VALUES, and a config file that can hold "
    "a value is a config file that will eventually hold a credential. Forward the variable by "
    "NAME with env_grant instead."
)

#: Environment variable naming out of POSIX: uppercase, digits, underscore, not leading-digit.
def _is_env_name(value: str) -> bool:
    return (
        value != ""
        and not value[0].isdigit()
        and all(character.isupper() or character.isdigit() or character == "_" for character in value)
    )


def _error(pointer: str, message: str) -> GraphValidationError:
    return GraphValidationError("provider_catalog", pointer, message)


def _string_list(raw: object, pointer: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise _error(pointer, "must be a list of strings")
    return tuple(str(item) for item in raw)


def profile_from_mapping(name: str, raw: object, *, pointer: str) -> CliProfile:
    """Validate one catalog entry into a ``CliProfile``. Fails closed on anything unrecognised."""
    if not isinstance(raw, Mapping):
        raise _error(pointer, "a provider entry must be a table")
    if "set_env" in raw:
        raise _error(f"{pointer}/set_env", _SET_ENV_REFUSED)
    unknown = sorted(set(raw) - _ALLOWED_KEYS)
    if unknown:
        raise _error(
            pointer,
            f"unknown key(s) {unknown}; a silently ignored key would leave you believing this "
            f"provider is configured when it is not. Known keys: {sorted(_ALLOWED_KEYS)}",
        )
    if "binary" not in raw:
        raise _error(f"{pointer}/binary", "a provider entry must name the binary to run")
    for key in sorted(_STRING_KEYS & set(raw)):
        if not isinstance(raw[key], str):
            raise _error(f"{pointer}/{key}", "must be a string")

    for key in sorted(set(raw) & {"unset_env", "env_grant"}):
        for index, entry in enumerate(_string_list(raw[key], f"{pointer}/{key}")):
            if not _is_env_name(entry):
                raise _error(
                    f"{pointer}/{key}/{index}",
                    f"{entry!r} is not an environment variable NAME. This field forwards names, "
                    "never values — if that was a credential, remove it and rotate it.",
                )

    envelope = str(raw.get("envelope", ""))
    if envelope and envelope not in ENVELOPE_PARSERS:
        raise _error(
            f"{pointer}/envelope",
            f"no parser named {envelope!r} ships with this version (known: "
            f"{sorted(ENVELOPE_PARSERS)}). An unreadable envelope is refused here rather than at "
            "run time, where it would already have cost a provider call.",
        )
    if envelope and not raw.get("usage_args"):
        raise _error(
            f"{pointer}/usage_args",
            f"envelope {envelope!r} is declared but no usage_args ask the CLI for it, so the CLI "
            "would emit plain text and every attempt would fail on an unreadable envelope.",
        )

    prompt_via = str(raw.get("prompt_via", "stdin"))
    if prompt_via not in ("stdin", "arg"):
        raise _error(f"{pointer}/prompt_via", "prompt_via must be 'stdin' or 'arg'")
    if any(word in name.lower() for word in _SECRET_WORDS):
        raise _error(f"{pointer}", "a provider name must not be secret-shaped")

    return CliProfile(
        binary=str(raw["binary"]),
        args=_string_list(raw.get("args", []), f"{pointer}/args"),
        prompt_via=prompt_via,
        unset_env=_string_list(raw.get("unset_env", []), f"{pointer}/unset_env"),
        env_grant=_string_list(raw.get("env_grant", []), f"{pointer}/env_grant"),
        usage_args=_string_list(raw.get("usage_args", []), f"{pointer}/usage_args"),
        envelope=envelope,
    )


def catalog_from_mapping(document: Mapping[str, object]) -> Mapping[str, CliProfile]:
    """Read the ``[providers.*]`` table of an already-parsed catalog document."""
    raw_providers = document.get("providers", {})
    if not isinstance(raw_providers, Mapping):
        raise _error("/providers", "the providers section must be a table")
    unknown_sections = sorted(set(document) - {"providers"})
    if unknown_sections:
        raise _error("/", f"unknown top-level section(s) {unknown_sections}; expected only 'providers'")
    return {
        name: profile_from_mapping(name, entry, pointer=f"/providers/{name}")
        for name, entry in sorted(raw_providers.items())
    }


def load_provider_catalog(path: Path) -> Mapping[str, CliProfile]:
    """Parse and validate a provider catalog file."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise _error("/", f"provider catalog {str(path)!r} could not be read: {exc}") from exc
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise _error("/", f"provider catalog {str(path)!r} is not valid TOML: {exc}") from exc
    return catalog_from_mapping(document)


def resolve_cli_profiles(
    *,
    catalog_path: Path | None = None,
    shipped: Mapping[str, CliProfile] = CLI_PROFILES,
    include_plugins: bool = True,
) -> Mapping[str, CliProfile]:
    """The profile map a run should use, in order of increasing authority.

    **plugins < shipped < operator catalog.**

    Third-party packages go first because they are the least deliberate choice — a transitive
    dependency can install one. Shipped profiles beat them so no package can quietly become the
    provider an existing graph already binds to (``provider_plugins`` refuses that outright; this
    ordering is the belt to that suspenders). The operator's catalog wins over everything, because
    an operator pointing ``claude`` at their own wrapper, or correcting a flag this version got
    wrong on their host, should not have to wait for a release.
    """
    # Imported here rather than at module scope: ``provider_plugins`` imports this module for its
    # validator, and a top-level import each way is a cycle.
    from bounded_loops.graph.adapters.connectors.provider_plugins import load_provider_plugins

    plugins = load_provider_plugins(shipped=shipped) if include_plugins else {}
    catalog = load_provider_catalog(catalog_path) if catalog_path is not None else {}
    return {**plugins, **shipped, **catalog}


def describe(profiles: Mapping[str, CliProfile]) -> tuple[str, ...]:
    """One human line per provider, for ``bl graph providers``. Names only — no values."""
    lines: list[str] = []
    for name in sorted(profiles):
        profile = profiles[name]
        metering = f"metered via {profile.envelope!r} envelope" if profile.envelope else "NOT metered"
        grants = ", ".join(profile.env_grant) if profile.env_grant else "none"
        lines.append(
            f"{name}: binary={profile.binary} prompt_via={profile.prompt_via} "
            f"{metering}; env names requested: {grants}"
        )
    return tuple(lines)
