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

**A catalog never carries a credential.** ``env_grant`` and ``unset_env`` hold NAMES, and
``set_env`` — which would hold values — is refused outright. ``args`` is the one field that
legitimately carries values (``--model gpt-5``), so it cannot be name-only; instead a credential
FLAG (``--api-key``, ``--token``, …) is refused there, because a key on a command line is visible
to every process on the host whether or not the value sits in this file. The engine's job is to
decide which names reach a subprocess — it has never needed to read a value and must not learn how.

The P3 audit is why that paragraph is this specific: the first version claimed "a catalog never
carries a credential" while ``args = ["--api-key", "sk-…"]`` loaded without complaint.

**An unknown key is an error, not a shrug.** The schema is closed. A typo'd ``envelop`` that
was silently ignored would leave the operator believing their provider is metered while every
spend cap on it fails closed as unmeasurable — the failure that looks exactly like protection
and is not.
"""

from __future__ import annotations

import os
from pathlib import Path
import stat
import tomllib
from typing import Mapping

from bounded_loops.graph.adapters.connectors.cli_envelope import ENVELOPE_PARSERS
from bounded_loops.graph.adapters.connectors.local_cli_worker import CLI_PROFILES, CliProfile
from bounded_loops.graph.domain.errors import GraphValidationError

#: Every key a catalog entry may declare. Mirrors the constructor arguments of ``CliProfile``
#: minus ``set_env`` — see ``_SET_ENV_REFUSED`` below for why that one is not operator-writable.
_ALLOWED_KEYS = frozenset({
    "binary", "args", "prompt_via", "unset_env", "usage_args", "envelope", "env_grant",
    # Task #68. An operator's own CLI can diverge exactly as agy does — needing a flag
    # after the prompt, or an explicit workspace because it does not use the process cwd.
    # Without these keys the catalog could describe every shipped provider except the one
    # that most needed describing.
    "post_prompt_args", "workspace_arg",
})
_LIST_KEYS = frozenset({"args", "unset_env", "usage_args", "env_grant", "post_prompt_args"})
_STRING_KEYS = frozenset({"binary", "prompt_via", "envelope", "workspace_arg"})

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


#: Flag STEMS that mean "the next argv word is a credential". Matched as a suffix on the flag name,
#: never as a substring of it — the first version used the ``_SECRET_WORDS`` substring test and
#: refused ``--max-tokens``, one of the commonest flags an agent CLI takes. A lint that rejects
#: ordinary configuration gets switched off, and then it protects nothing.
_CREDENTIAL_FLAG_STEMS = (
    "api_key", "apikey", "auth", "auth_token", "access_token", "bearer", "credential",
    "credentials", "client_secret", "password", "passwd", "secret", "token",
)
#: Words that make a flag a QUANTITY, not a credential: ``--max-tokens``, ``--token-limit``,
#: ``--num-tokens``. Same distinction ``validate_graph._is_declared_quantity`` already draws for
#: budget fields, for the same reason.
_QUANTITY_WORDS = ("max", "min", "num", "count", "limit", "budget", "size", "length", "total")


def _is_credential_flag(entry: str) -> bool:
    """Does this argv word pass a credential VALUE on the command line?

    Only flags are considered — a bare value cannot be judged, and guessing at value shapes is how
    ``--model sk-experiment`` would become collateral damage.
    """
    if not entry.startswith("-"):
        return False
    flag = entry.lstrip("-").replace("-", "_").split("=", 1)[0].lower()
    if not flag:
        return False
    parts = flag.split("_")
    if any(word in parts for word in _QUANTITY_WORDS):
        return False
    return flag in _CREDENTIAL_FLAG_STEMS or any(
        flag.endswith("_" + stem) for stem in _CREDENTIAL_FLAG_STEMS
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

    # argv is the one field that legitimately carries VALUES — ``--model gpt-5``, ``--region in`` —
    # so it cannot be name-only like env_grant. What it must not carry is a credential, and
    # ``--api-key <anything>`` in a config file is unambiguously that. Refused on the FLAG name
    # rather than by guessing at the value's shape, which keeps ``--model sk-experiment`` working.
    argv = _string_list(raw.get("args", []), f"{pointer}/args")
    for index, entry in enumerate(argv):
        if _is_credential_flag(entry):
            raise _error(
                f"{pointer}/args/{index}",
                f"{entry!r} passes a credential on the command line. Even if the value is not in "
                "this file, argv is visible to every process on the host. Have the CLI read it from "
                "an environment variable and forward that NAME with env_grant instead.",
            )

    prompt_via = str(raw.get("prompt_via", "stdin"))
    if prompt_via not in ("stdin", "arg"):
        raise _error(f"{pointer}/prompt_via", "prompt_via must be 'stdin' or 'arg'")
    if any(word in name.lower() for word in _SECRET_WORDS):
        raise _error(f"{pointer}", "a provider name must not be secret-shaped")

    return CliProfile(
        binary=str(raw["binary"]),
        args=argv,
        prompt_via=prompt_via,
        unset_env=_string_list(raw.get("unset_env", []), f"{pointer}/unset_env"),
        env_grant=_string_list(raw.get("env_grant", []), f"{pointer}/env_grant"),
        usage_args=_string_list(raw.get("usage_args", []), f"{pointer}/usage_args"),
        envelope=envelope,
        post_prompt_args=_string_list(
            raw.get("post_prompt_args", []), f"{pointer}/post_prompt_args",
        ),
        workspace_arg=str(raw.get("workspace_arg", "")),
    )


def catalog_from_mapping(document: Mapping[str, object]) -> Mapping[str, CliProfile]:
    """Read the ``[providers.*]`` table of an already-parsed catalog document."""
    raw_providers = document.get("providers", {})
    if not isinstance(raw_providers, Mapping):
        raise _error("/providers", "the providers section must be a table")
    unknown_sections = sorted(set(document) - {"providers"})
    if unknown_sections:
        raise _error("/", f"unknown top-level section(s) {unknown_sections}; expected only 'providers'")
    for name in raw_providers:
        # ``catalog_from_mapping`` is public and does not only see TOML — a caller passing a dict
        # can supply an empty or non-string key. An empty name yields a provider no binding can
        # ever reference, which then lists in ``bl graph providers`` as a nameless row; a non-string
        # key used to surface as ``AttributeError`` from ``name.lower()`` rather than a validation
        # error the caller can act on.
        if not isinstance(name, str) or not name.strip():
            raise _error("/providers", f"provider name {name!r} must be a non-empty string")
    return {
        name: profile_from_mapping(name, entry, pointer=f"/providers/{name}")
        for name, entry in sorted(raw_providers.items())
    }


#: A provider catalog is a handful of small tables. A megabyte is already three orders of magnitude
#: more than any real one, so the cap costs nothing legitimate and bounds the work a bad path (or a
#: mistakenly-pointed ``BOUNDED_LOOPS_PROVIDERS``) can make the parser do before it fails.
_MAX_CATALOG_BYTES = 1024 * 1024


def load_provider_catalog(path: Path) -> Mapping[str, CliProfile]:
    """Parse and validate a provider catalog file."""
    try:
        info = path.stat()
        # A FIFO or a character device reports st_size == 0, so a cap on the STAT slipped straight
        # past both and ``read_bytes()`` then blocked forever — a hang primitive against resume,
        # approve and the console, reachable from a recorded catalog path or the env var. A catalog
        # is a regular file; anything else is refused before it is opened.
        if not stat.S_ISREG(info.st_mode):
            raise _error(
                "/",
                f"provider catalog {str(path)!r} is not a regular file. A catalog is a small TOML "
                "file; a pipe or device here would block the run instead of configuring it.",
            )
        if info.st_size > _MAX_CATALOG_BYTES:
            raise _error(
                "/",
                f"provider catalog {str(path)!r} is {info.st_size} bytes, over the "
                f"{_MAX_CATALOG_BYTES}-byte limit. A real catalog is a few small tables; this is "
                "almost certainly the wrong file.",
            )
        # Bounded by the bytes actually READ, not by what stat promised: the size can grow between
        # the two calls, and a stat-only cap is a check on a number rather than on the data.
        with path.open("rb") as handle:
            raw = handle.read(_MAX_CATALOG_BYTES + 1)
        if len(raw) > _MAX_CATALOG_BYTES:
            raise _error(
                "/",
                f"provider catalog {str(path)!r} exceeded the {_MAX_CATALOG_BYTES}-byte limit while "
                "being read.",
            )
    except OSError as exc:
        raise _error("/", f"provider catalog {str(path)!r} could not be read: {exc}") from exc
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise _error("/", f"provider catalog {str(path)!r} is not valid TOML: {exc}") from exc
    return catalog_from_mapping(document)


#: Machine-wide catalog. An env var as well as a flag because the person who configures a machine's
#: providers is usually not the person typing the command — and because a provider set only a flag
#: can express is a provider set that cannot be RESUMED: `bl graph resume` would open the run with a
#: different provider map than the one the run was created with.
PROVIDERS_ENV_VAR = "BOUNDED_LOOPS_PROVIDERS"


def default_catalog_path() -> Path | None:
    """The catalog from ``BOUNDED_LOOPS_PROVIDERS``, or ``None``. Read explicitly by callers rather
    than implicitly inside ``resolve_cli_profiles`` — a function that silently consults the
    environment cannot be reasoned about from its arguments."""
    raw = os.environ.get(PROVIDERS_ENV_VAR, "").strip()
    return Path(raw) if raw else None


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
    from bounded_loops.graph.adapters.connectors.provider_plugins import (
        load_provider_plugins,
        reconstructed,
    )

    # ``shipped`` is snapshotted BEFORE plugin code runs and the resolved map is built from that
    # snapshot, never from the live module global. The P3 audit showed why: a plugin factory that
    # mutates ``CLI_PROFILES`` and returns something harmless replaced the shipped ``claude`` with
    # its own binary, past every check that inspected only the returned mapping.
    #
    # ``reconstructed``, not ``dict()``: a shallow copy holds the SAME ``CliProfile`` objects, and
    # ``object.__setattr__`` on one of those walks past both ``frozen=True`` and ``MappingProxyType``
    # — so round two of the audit poisoned the snapshot as well. Fresh values leave nothing shared.
    baseline = reconstructed(shipped)
    plugins = load_provider_plugins(shipped=baseline) if include_plugins else {}
    catalog = load_provider_catalog(catalog_path) if catalog_path is not None else {}
    return {**plugins, **baseline, **catalog}


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
