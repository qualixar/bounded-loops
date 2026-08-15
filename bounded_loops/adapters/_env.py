"""
Shared subprocess-environment allowlist — the single source of truth.

Hardening: the env allowlist was duplicated verbatim in
9 files (every runner, every subprocess gate, and the Stop hook). All copies
were identical, but nothing connected them — a future maintainer adding a
variable to one file would silently leave the others with a permissive gap,
and the allowlist is the PRIMARY secret-exfiltration defense. Hoisted here;
every subprocess-spawning module imports from this one place.

Hardening: `build_subprocess_env` also sanitizes PATH to
absolute directories only. Gate/runner subprocesses run with `cwd=workspace`;
if the parent PATH contains "." or a relative entry, a workspace-local binary
(e.g. a no-op `pytest` a malicious loop shipped in its seed/) would shadow the
real system binary and could force a false pass. Dropping non-absolute PATH
entries closes that cwd-relative shadowing without affecting any legitimate
(absolute) PATH directory.
"""
from __future__ import annotations

import os
from typing import Mapping

# The variables a subprocess genuinely needs. NEVER widen this without a
# security review — every entry is a potential exfiltration channel.
#
# ``USER`` added 2026-08-16 after bisecting a live failure, and it is the one
# entry here that deserves its reasoning written down rather than assumed.
#
# SYMPTOM: on a host whose agent CLI is under enterprise-managed settings, every
# CLI runner returned EMPTY stdout and ``tokens=0``. The loop then burned its
# whole ``max_iterations`` budget making no progress, and reported HALT — a
# perfectly well-behaved bound doing exactly the wrong thing, because the agent
# had never run at all. Nothing in the receipt log said why.
#
# CAUSE: with this allowlist applied, the CLI printed "Your organization
# requires remote managed settings to load, but they could not be loaded."
# Bisected one variable at a time against the real binary: ``USER`` alone
# restores it (``is_error: false``, real usage, real cost); ``LOGNAME`` alone
# does NOT. Only ``USER`` is added, because only ``USER`` was shown to be needed.
#
# SECURITY ARGUMENT, since this is the primary exfiltration defense: ``USER``
# carries the OS account name and nothing else — it is not a credential and
# matches no ``SENSITIVE_ENV_MARKERS`` pattern. It also discloses nothing the
# child does not already hold: ``HOME`` has been on this list since it was
# written, and on every supported platform ``HOME`` contains the same account
# name as its final path component. So this widens the identity surface by
# zero. That argument is the bar for any future addition here; "the tool seems
# to want it" is not.
ENV_ALLOWLIST = frozenset({"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SHELL", "USER"})
SENSITIVE_ENV_MARKERS = (
    "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "API_KEY", "AUTH",
)


# ── Operator env-passthrough grant: ONE name for the whole product ────────────────────────
#
# Until P3 there were two variables for one security concern, and — worse than the duplicate
# name — they did not mean the same thing:
#
#   BOUNDED_LOOPS_ENV_PASSTHROUGH_ALLOW  (base engine)  operator allow ∩ manifest request
#   BOUNDED_LOOPS_CLI_ENV_GRANT          (graph CLI)    operator allow ∪ profile grant
#
# The base engine required the WORKLOAD to also ask for the variable; the graph CLI let the
# operator grant alone forward it. Unifying only the name would have given one variable two
# different meanings depending on which subsystem read it — the kind of surprise that ends with
# a credential in a subprocess nobody meant to give it to.
#
# So both are unified: one canonical name, and the intersection semantics, everywhere. A
# variable now reaches a child process only when the workload DECLARES it (manifest
# ``env_passthrough`` / provider-catalog ``env_grant``) *and* the operator ALLOWS it here. Two
# independent keys, held in two different places — a committed file and an ambient variable — so
# neither the graph author nor the operator can open the channel alone.
ENV_PASSTHROUGH_ALLOW_VAR = "BOUNDED_LOOPS_ENV_PASSTHROUGH_ALLOW"
#: Deprecated alias, still honoured for the local-CLI path that shipped it, so an operator who
#: already set it does not silently lose their grant on upgrade. Never read by the base engine:
#: honouring a graph-specific name there would widen a subsystem that never had it.
LEGACY_CLI_ENV_GRANT_VAR = "BOUNDED_LOOPS_CLI_ENV_GRANT"


def operator_env_grants(
    source: Mapping[str, str] | None = None,
    *,
    include_legacy_cli_alias: bool = False,
) -> frozenset[str]:
    """Environment variable NAMES the operator has authorized for passthrough.

    Default-closed: an unset or empty variable authorizes nothing, whatever a workload requests.
    Returns names only — this function never reads a value, and no caller should ask it to.
    """
    env = os.environ if source is None else source
    names: set[str] = set()
    variables = [ENV_PASSTHROUGH_ALLOW_VAR]
    if include_legacy_cli_alias:
        variables.append(LEGACY_CLI_ENV_GRANT_VAR)
    for variable in variables:
        raw = env.get(variable, "")
        names.update(name.strip() for name in raw.split(",") if name.strip())
    return frozenset(names)


def sanitize_path(path_value: str) -> str:  # public: also used by the graph local-CLI connector
    """Keep only ABSOLUTE directory entries. Drops "", ".", and any relative
    entry — the vectors by which a `cwd=workspace` subprocess could resolve a
    workspace-local binary shadow."""
    kept = [p for p in path_value.split(os.pathsep) if p and os.path.isabs(p)]
    return os.pathsep.join(kept)


def build_subprocess_env(ctx_env: dict[str, str] | None = None) -> dict[str, str]:
    """Build the env dict for a subprocess: the allowlisted parent vars (with
    PATH sanitized to absolute entries) plus any explicit ctx.env opt-ins
    merged over the top. Never leaks the full parent environment."""
    base = {k: v for k, v in os.environ.items() if k in ENV_ALLOWLIST}
    if "PATH" in base:
        base["PATH"] = sanitize_path(base["PATH"])
    if ctx_env:
        return {**base, **ctx_env}
    return base


def output_redactions(ctx_env: dict[str, str]) -> tuple[str, ...]:
    """Values explicitly configured as secrets that must never reach logs."""
    return tuple(
        value
        for name, value in ctx_env.items()
        if value and any(marker in name.upper() for marker in SENSITIVE_ENV_MARKERS)
    )
