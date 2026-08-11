"""Connector mode — subscription CLI (DEFAULT) vs BYOK/https. Informational only.

DELIBERATE SCOPE BOUNDARY: this module writes NOTHING to disk. There is today no
persisted "connector config" contract anywhere else in this engine to write INTO —
`local_cli` node resolution happens per-graph via `CLI_PROFILES`/a
`LocalCliConnectorPort` resolver, and BYOK/https admission happens per-run via the
existing `--admitted <connections.json>` mechanism (`cli_graph.py`'s `run`
subcommand). Inventing a new global connector-config file with no reader would be
scope creep this task explicitly forbids ("do NOT build a credential store"). This
module's only job is the interactive prompt plus an honest pointer to where BYOK
setup actually happens today — never a secret value, never a file write.
"""

from __future__ import annotations

from enum import Enum
from typing import Callable


class ConnectorMode(str, Enum):
    """How a graph's connector nodes authenticate — chosen at `bl graph init` time
    purely to guide the user; never persisted by this package."""

    LOCAL_CLI = "local_cli"
    BYOK = "byok"


_CONNECTOR_PROMPT_TEXT = (
    "Connector mode — how your graph's connector nodes authenticate:\n"
    "  [1] subscription CLI (DEFAULT) — use your own logged-in CLI (claude/codex/grok/...) — zero setup\n"
    "  [2] BYOK / https — bring your own API key via an admitted https connection\n"
)

_CONNECTOR_ALIASES: dict[str, ConnectorMode] = {
    "1": ConnectorMode.LOCAL_CLI,
    "local_cli": ConnectorMode.LOCAL_CLI,
    "local-cli": ConnectorMode.LOCAL_CLI,
    "subscription": ConnectorMode.LOCAL_CLI,
    "2": ConnectorMode.BYOK,
    "byok": ConnectorMode.BYOK,
    "https": ConnectorMode.BYOK,
}


def prompt_connector_mode(
    *,
    input_fn: Callable[[str], str] = input,
    default: ConnectorMode = ConnectorMode.LOCAL_CLI,
) -> ConnectorMode:
    """Prompt for connector mode, looping on an unrecognized answer rather than
    guessing. Blank input accepts *default* — the zero-friction path."""
    print(_CONNECTOR_PROMPT_TEXT, end="")
    while True:
        raw = input_fn(f"Choose 1-2, or type the name [{default.value}]: ").strip().lower()
        if raw == "":
            return default
        choice = _CONNECTOR_ALIASES.get(raw)
        if choice is not None:
            return choice
        print("Please enter 1, 2, local_cli, or byok.")


def describe_byok_pointer() -> str:
    """Guidance printed when BYOK is chosen. Names the ENV VAR mechanism and the
    existing `--admitted` record shape; never prints, requests, or stores a
    credential value. The one `export ...=` example line is an unmistakable
    placeholder (`...`), never a real-looking secret."""
    return (
        "BYOK / https connector mode selected. This installer NEVER stores credentials.\n"
        "To admit a BYOK connection for `bl graph run`, set your credential in an\n"
        "environment variable (never in a file), then reference that ENV VAR NAME —\n"
        "never its value — in an --admitted <connections.json> record when you run the\n"
        "graph:\n"
        "  export MY_API_KEY=...          # your shell only; never written to disk here\n"
        "  bl graph run --admitted connections.json ...\n"
        "See `bl graph run --help` for the --admitted record shape (endpoint, the\n"
        "credential ENV-VAR name, expiry, and request style — never the credential\n"
        "value itself)."
    )
