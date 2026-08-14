"""``bl graph providers`` — what agent CLIs can this deployment actually run?

Split out of ``cli_graph.py`` to keep that file under the 800-line cap, following the pattern
``cli_graph_resume.py`` and ``cli_graph_approve.py`` already established. Holds the catalog-path
resolution shared by ``providers`` and ``run``, so both agree on where a provider catalog comes
from — two copies of that precedence would be a silent divergence.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from bounded_loops.graph.adapters.connectors.provider_catalog import (
    default_catalog_path,
    describe as _describe_providers,
    resolve_cli_profiles,
)
from bounded_loops.graph.domain.errors import GraphValidationError


def _err(msg: str) -> None:
    import sys

    print(f"error: {msg}", file=sys.stderr)


def _catalog_path(args: argparse.Namespace) -> Path | None:
    """The provider catalog to use: ``--providers``, else ``BOUNDED_LOOPS_PROVIDERS``, else none.

    An env var as well as a flag because the operator who configures providers is often not the
    person typing the command — and a provider set that only a flag can express cannot be made
    the default for a whole machine.
    """
    explicit = getattr(args, "providers", None)
    return Path(explicit) if explicit else default_catalog_path()


def cmd_graph_providers(args: argparse.Namespace) -> int:
    """bl graph providers — what agent CLIs can this deployment actually run?

    Answers the question that used to require reading ``CLI_PROFILES`` in the source: which
    providers exist, which of them can report what they spent, and which environment variable
    NAMES each asks to receive. A spend cap on an unmetered provider fails closed, so knowing
    which column a provider is in before authoring a graph is the difference between a budget
    that works and a run that pays and then refuses.
    """
    try:
        profiles = resolve_cli_profiles(catalog_path=_catalog_path(args))
    except GraphValidationError as exc:
        _err(f"graph providers: catalog rejected — [{exc.code}] {exc.pointer} — {exc.message}")
        return 2
    if getattr(args, "json", False):
        print(json.dumps({
            "providers": {
                name: {
                    "binary": profile.binary,
                    "prompt_via": profile.prompt_via,
                    "metered": bool(profile.envelope),
                    "envelope": profile.envelope,
                    "env_names_requested": list(profile.env_grant),
                }
                for name, profile in sorted(profiles.items())
            },
        }, indent=2, sort_keys=True))
        return 0
    for line in _describe_providers(profiles):
        print(line)
    return 0


def add_providers_parser(graph_subs: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Register ``bl graph providers``."""
    providers_p = graph_subs.add_parser(
        "providers",
        help="List the agent CLIs this deployment can run.",
        description=(
            "Show every provider wired into this deployment: the shipped profiles, any added by "
            "an installed provider package, and any added or overridden by --providers. Prints "
            "binary, prompt mode, whether spend on it can be METERED, and which environment "
            "variable NAMES it asks to receive. No values are ever read or printed."
        ),
    )
    providers_p.add_argument("--providers", default=None, metavar="<catalog.toml>",
                             help="Provider catalog to merge over the shipped profiles.")
    providers_p.add_argument("--json", action="store_true", help="Emit JSON output.")
    providers_p.set_defaults(func=cmd_graph_providers)
