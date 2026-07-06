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

# The six variables a subprocess genuinely needs. NEVER widen this without a
# security review — every entry is a potential exfiltration channel.
ENV_ALLOWLIST = frozenset({"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SHELL"})


def _sanitize_path(path_value: str) -> str:
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
        base["PATH"] = _sanitize_path(base["PATH"])
    if ctx_env:
        return {**base, **ctx_env}
    return base
