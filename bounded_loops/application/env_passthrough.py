"""Resolving a manifest's ``env_passthrough`` request against the operator's allow-list.

Extracted from ``composition.py`` in P3 (size cap) — but the split earns its keep on its own: this
is the product's credential-authorization rule, and it was buried at the bottom of a 800-line
wiring file where nobody looking for "how does a secret reach a runner" would find it.

The rule, unchanged and now shared with the graph engine's local-CLI path: a variable reaches a
child process only when the WORKLOAD names it (``runner.env_passthrough`` in loop.yaml) *and* the
OPERATOR allows it (``BOUNDED_LOOPS_ENV_PASSTHROUGH_ALLOW``). Two independent keys. Default-closed:
an unset allow-list authorizes nothing, whatever the manifest requests.

Both failure modes are loud. A name the operator never allowed is refused. So is a name the
operator DID allow that is simply absent from the environment — because injecting ``None`` or an
empty string there produces an authentication failure inside whatever tool the runner launches,
several layers away from the missing variable that actually caused it.
"""

from __future__ import annotations

import os

from bounded_loops.adapters._env import ENV_PASSTHROUGH_ALLOW_VAR, operator_env_grants
from bounded_loops.application.manifest import LoopManifest
from bounded_loops.domain.errors import ManifestError


def resolve_env_passthrough(manifest: LoopManifest) -> dict[str, str]:
    """
    Resolves manifest.env_passthrough
    into real values, gated by an OPERATOR-level allowlist — the actual
    authorization control  explicitly deferred to this wiring.
    Default-closed: if the operator allowlist var is unset/empty, NO
    env_passthrough entry is ever passed through, regardless of what any
    loop.yaml requests. A loop naming a var outside the operator allowlist,
    or a var the allowlist permits but that is absent from the real
    environment, both FAIL CLOSED with a clear ManifestError — never a
    silent None-injection or an opaque downstream tool auth failure.
    """
    if not manifest.env_passthrough:
        return {}
    operator_allowed = {
        # Shared with the graph engine's local-CLI path since P3 — one canonical variable, one
        # set of intersection semantics. This subsystem does NOT honour the graph-specific legacy
        # alias: widening the base engine by a name it never read would be a security regression
        # dressed up as a cleanup.
        *operator_env_grants(),
    }
    resolved: dict[str, str] = {}
    for name in manifest.env_passthrough:
        if name not in operator_allowed:
            raise ManifestError(
                f"loop.yaml requests runner.env_passthrough: {name}, but it is not in "
                f"the operator allowlist ({ENV_PASSTHROUGH_ALLOW_VAR}). "
                "Refusing to pass through an unauthorized variable."
            )
        if name not in os.environ:
            raise ManifestError(
                f"loop.yaml requests runner.env_passthrough: {name}, and it is operator-"
                "allowlisted, but it is not set in the current environment. Refusing to "
                "launch with a missing credential rather than run unauthenticated."
            )
        resolved[name] = os.environ[name]
    return resolved
