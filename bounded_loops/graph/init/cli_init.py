"""`bl graph init` — interactive installer for egress posture + connector mode.

Orchestrates the other modules in this package: gathers connector mode + egress
posture + (if applicable) allowlist hosts — from `--flags` when given, or by
prompting interactively — shows a confirmation summary, writes
`~/.bounded-loops/egress.json` (or `--config <path>`) securely, and proves the
write by reading it straight back through `egress_posture.resolve_egress_posture`,
the exact fail-closed reader `bl graph run` consumes.

DEFAULT is OPEN egress + subscription-CLI connector whenever a prompt is
accepted blank or a flag is omitted — the frictionless path, never lockdown by
accident. An existing config is always shown and confirmed before being
replaced; a symlink at the config path is always refused, never followed or
silently replaced, in every mode including `--yes`.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Callable, Mapping

from bounded_loops.graph.adapters.enforcement.egress_posture import EgressPosture, EgressPostureConfig
from bounded_loops.graph.domain.errors import GraphValidationError
from bounded_loops.graph.init.config_writer import (
    ExistingConfig,
    build_config_payload,
    canonicalize_allowlist_entries,
    default_config_path,
    flatten_allowlist_flag,
    read_existing_snapshot,
    resolve_config_path,
    verify_round_trip,
    write_config_atomically,
)
from bounded_loops.graph.init.connector import ConnectorMode, describe_byok_pointer, prompt_connector_mode
from bounded_loops.graph.init.errors import GraphInitError
from bounded_loops.graph.init.prompts import (
    confirm_overwrite,
    confirm_write,
    prompt_allowlist_hosts,
    prompt_egress_posture,
)

_AMBIENT_ENV_VARS = ("BOUNDED_LOOPS_EGRESS_POSTURE", "BOUNDED_LOOPS_EGRESS_ALLOWLIST")


def _err(msg: str) -> None:
    print(f"error: graph init: {msg}", file=sys.stderr)


def cmd_graph_init(args: argparse.Namespace, *, input_fn: Callable[[str], str] = input) -> int:
    """bl graph init [--posture ...] [--allowlist ...] [--connector ...] [--yes] [--config <path>].

    ``input_fn`` is never supplied by argparse's own ``args.func(args)`` dispatch
    (real runs read real stdin via the builtin ``input``); tests inject a stub so
    the full interactive wizard is provable without any real interactive I/O.
    """
    try:
        return _run_init(args, input_fn=input_fn, environ=os.environ)
    except EOFError:
        _err("no interactive input available; re-run with --posture/--connector/--yes for non-interactive use")
        return 2
    except KeyboardInterrupt:
        print("\nAborted — no changes were made.")
        return 1


def _run_init(args: argparse.Namespace, *, input_fn: Callable[[str], str], environ: Mapping[str, str]) -> int:
    yes = bool(getattr(args, "yes", False))
    posture_flag = getattr(args, "posture", None)
    connector_flag = getattr(args, "connector", None)
    allowlist_values: list[str] = list(getattr(args, "allowlist", None) or [])
    non_interactive = yes or posture_flag is not None or connector_flag is not None or bool(allowlist_values)

    config_path = resolve_config_path(getattr(args, "config", None), environ)

    try:
        existing = read_existing_snapshot(config_path)
    except GraphInitError as exc:
        _err(str(exc))
        return 2

    gate = _handle_existing_config(
        existing, config_path, non_interactive=non_interactive, yes=yes, input_fn=input_fn,
    )
    if gate is not None:
        return gate

    connector_mode = _resolve_connector_mode(connector_flag, non_interactive=non_interactive, input_fn=input_fn)
    if connector_mode is ConnectorMode.BYOK:
        print(describe_byok_pointer())

    posture = _resolve_posture(posture_flag, non_interactive=non_interactive, input_fn=input_fn)

    if allowlist_values and posture is not EgressPosture.ALLOWLIST:
        _err(
            f"--allowlist was given but the resolved posture is '{posture.value}' — "
            "allowlist hosts only apply with --posture allowlist."
        )
        return 2

    try:
        hosts = _resolve_allowlist_hosts(posture, allowlist_values, non_interactive=non_interactive, input_fn=input_fn)
    except GraphInitError as exc:
        _err(str(exc))
        return 2

    payload = build_config_payload(posture, hosts)

    if not non_interactive:
        _print_summary(config_path, connector_mode, posture, hosts)
        if not confirm_write(input_fn=input_fn):
            print("Aborted — no changes were made.")
            return 1

    try:
        write_config_atomically(config_path, payload)
    except GraphInitError as exc:
        _err(str(exc))
        return 2

    try:
        verified = verify_round_trip(config_path)
    except GraphValidationError as exc:
        # Should be unreachable: build_config_payload only ever emits shapes this
        # package's own writer produces. Treated as an internal bug, not a user
        # input error, if it ever fires — never claim success on a bad write.
        _err(f"internal error — the written config failed its own reader: {exc}")
        return 2

    _print_written_confirmation(config_path, verified, environ)
    return 0


# ── field resolution (flag > non-interactive default > interactive prompt) ─────


def _handle_existing_config(
    existing: ExistingConfig | None,
    config_path: Path,
    *,
    non_interactive: bool,
    yes: bool,
    input_fn: Callable[[str], str],
) -> int | None:
    """Returns an exit code to return immediately, or ``None`` to keep going."""
    if existing is None:
        return None
    if existing.is_symlink:
        _err(f"'{config_path}' is a symlink; refusing to write through or replace it — remove it manually and re-run.")
        return 2
    _print_existing_summary(existing)
    if non_interactive:
        if yes:
            return None
        _err(
            f"an existing config was found at {config_path}; re-run with --yes to overwrite "
            "(or omit --posture/--connector/--allowlist to be prompted interactively)."
        )
        return 2
    if confirm_overwrite(input_fn=input_fn):
        return None
    print("Aborted — no changes were made.")
    return 1


def _resolve_connector_mode(
    connector_flag: str | None, *, non_interactive: bool, input_fn: Callable[[str], str],
) -> ConnectorMode:
    if connector_flag is not None:
        return ConnectorMode(connector_flag)
    if non_interactive:
        return ConnectorMode.LOCAL_CLI
    return prompt_connector_mode(input_fn=input_fn)


def _resolve_posture(
    posture_flag: str | None, *, non_interactive: bool, input_fn: Callable[[str], str],
) -> EgressPosture:
    if posture_flag is not None:
        return EgressPosture(posture_flag)
    if non_interactive:
        return EgressPosture.OPEN
    return prompt_egress_posture(input_fn=input_fn)


def _resolve_allowlist_hosts(
    posture: EgressPosture,
    allowlist_values: list[str],
    *,
    non_interactive: bool,
    input_fn: Callable[[str], str],
) -> tuple[str, ...]:
    if posture is not EgressPosture.ALLOWLIST:
        return ()
    if allowlist_values:
        return canonicalize_allowlist_entries(flatten_allowlist_flag(allowlist_values))
    if non_interactive:
        print("warning: --posture allowlist with no allowlist hosts denies ALL outbound egress for connector nodes.")
        return ()
    return prompt_allowlist_hosts(input_fn=input_fn)


# ── printing ──────────────────────────────────────────────────────────────────────


def _format_allowlist(config: EgressPostureConfig) -> str:
    hosts = ", ".join(f"{d.hostname}:{d.port}" for d in config.allowlist)
    return hosts or "(none)"


def _print_existing_summary(existing: ExistingConfig) -> None:
    if existing.error is not None:
        print(f"An existing (but INVALID) config was found at {existing.path}:")
        print(f"  {existing.error}")
        return
    assert existing.config is not None
    print(f"An existing config was found at {existing.path}:")
    print(f"  posture:   {existing.config.posture.value}")
    print(f"  allowlist: {_format_allowlist(existing.config)}")


def _print_summary(
    config_path: Path, connector_mode: ConnectorMode, posture: EgressPosture, hosts: tuple[str, ...],
) -> None:
    print("Configuration to write:")
    print(f"  path:           {config_path}")
    print(f"  connector mode: {connector_mode.value}")
    print(f"  egress posture: {posture.value}")
    print(f"  allowlist:      {', '.join(hosts) if hosts else '(none)'}")


def _print_written_confirmation(config_path: Path, verified: EgressPostureConfig, environ: Mapping[str, str]) -> None:
    print(f"Wrote {config_path}")
    detail = f", allowlist={_format_allowlist(verified)}" if verified.posture is EgressPosture.ALLOWLIST else ""
    print(f"Verified: bl graph run will resolve this file as posture={verified.posture.value}{detail}.")
    if config_path != default_config_path():
        print(
            f"NOTE: '{config_path}' is a NON-DEFAULT path — bl graph run only reads it if "
            f"BOUNDED_LOOPS_EGRESS_CONFIG={config_path} (or an equivalent override) is set; "
            f"otherwise it falls back to {default_config_path()}."
        )
    ambient = [name for name in _AMBIENT_ENV_VARS if environ.get(name, "").strip()]
    if ambient:
        print(
            f"NOTE: your current shell also exports {', '.join(ambient)} — "
            "environment variables take precedence over this file at runtime."
        )
