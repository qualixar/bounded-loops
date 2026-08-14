"""Who a local approval was decided BY, and how much that is worth.

The problem this fixes, stated plainly: a local approval receipt named the TENANT, not a person.
`SameTenantArenaAuthorizer` requires `subject_id == organization_id`, `actor_id` is set from
`subject_id`, and a local run's organization is the constant `local-org` — so every locally
approved irreversible effect produced a receipt saying `local-org` approved it. For a system whose
entire product is receipts you can trust, "somebody at this tenant said yes" is a weak thing to
be able to prove about an irreversible action.

**This does not make the identity trustworthy, and does not pretend to.** An OS username is
self-asserted; an environment variable is trivially set; a config file is written by whoever can
write the workspace. None of these is authentication. So the receipt records the identity AND
`decided_by_source` — where it came from — and never claims it was verified. A reader can then
judge what it is worth, which is the honest thing to offer and is strictly more than the previous
answer of nothing.

`actor_id` is untouched. It remains the authorization subject, because the authorizer's invariant
is what makes tenancy hold, and conflating "who is permitted" with "who decided" is how one of
them ends up wrong. Two questions, two fields.

Resolution order, most deliberate first:

1. `[identity] name` in `.bounded-loops/config.toml` — someone chose to write it down.
2. `BOUNDED_LOOPS_IDENTITY` — set for this process or this CI job.
3. The OS user.
4. `unknown`, when even that fails.
"""

from __future__ import annotations

import getpass
import os
from dataclasses import dataclass
from typing import Any, Mapping

#: The environment variable an operator or CI job can set for one process.
IDENTITY_ENV_VAR = "BOUNDED_LOOPS_IDENTITY"

#: Recorded when nothing at all could be resolved. A real value, not an empty string, so a
#: receipt reader sees "we did not know" rather than a blank that looks like a missing field.
UNKNOWN = "unknown"

#: Longest identity recorded. An identity is a name, not a payload; an unbounded one would be a
#: way to write arbitrary bytes into every approval receipt.
_MAX_LENGTH = 128

SOURCE_CONFIGURED = "configured"
SOURCE_ENVIRONMENT = "environment"
SOURCE_OS_USER = "os_user"
SOURCE_UNKNOWN = "unknown"

#: What each source is actually worth, in the words a receipt reader needs. Kept next to the
#: sources so the two cannot drift.
SOURCE_MEANING: Mapping[str, str] = {
    SOURCE_CONFIGURED: (
        "written in this project's .bounded-loops/config.toml by whoever can write that file. "
        "Deliberate, but not authenticated."
    ),
    SOURCE_ENVIRONMENT: (
        f"read from ${IDENTITY_ENV_VAR} in the approving process. Trivially set by anyone who "
        "can start that process. Not authenticated."
    ),
    SOURCE_OS_USER: (
        "the operating-system user running the approval. Self-asserted: it proves which local "
        "account acted, not which human. Not authenticated."
    ),
    SOURCE_UNKNOWN: (
        "could not be determined at all. The receipt records that nobody was identified, which "
        "is the honest answer and is not the same as nobody approving."
    ),
}


@dataclass(frozen=True)
class LocalIdentity:
    """A name for whoever decided, plus where the name came from.

    `verified` is a field rather than an omission because its value is the point: it is always
    False on a local run, and a surface that renders this must say so rather than presenting a
    name as if it were proven.
    """

    name: str
    source: str

    @property
    def verified(self) -> bool:
        """Always False locally. There is no local authentication to verify against."""
        return False

    @property
    def meaning(self) -> str:
        """One sentence a receipt reader can act on."""
        return SOURCE_MEANING.get(self.source, SOURCE_MEANING[SOURCE_UNKNOWN])

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready, for the receipt and for every surface that renders it."""
        return {
            "decided_by": self.name,
            "decided_by_source": self.source,
            "decided_by_verified": self.verified,
            "decided_by_meaning": self.meaning,
        }


def _clean(raw: object) -> str | None:
    """A usable identity, or None. Rejects rather than truncates an over-long name.

    Truncating would silently record a DIFFERENT person's name than the one supplied, which on an
    approval receipt is worse than declining to record one.
    """
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value or len(value) > _MAX_LENGTH:
        return None
    # Control characters would corrupt the JSONL receipt line they are written into.
    if any(character < " " or character == "\x7f" for character in value):
        return None
    return value


def resolve(config: Mapping[str, Any] | None = None) -> LocalIdentity:
    """Who is deciding, here, now. Never raises — an approval must not fail over a name."""
    identity_block = (config or {}).get("identity")
    if isinstance(identity_block, Mapping):
        configured = _clean(identity_block.get("name"))
        if configured is not None:
            return LocalIdentity(name=configured, source=SOURCE_CONFIGURED)

    from_environment = _clean(os.environ.get(IDENTITY_ENV_VAR))
    if from_environment is not None:
        return LocalIdentity(name=from_environment, source=SOURCE_ENVIRONMENT)

    try:
        os_user = _clean(getpass.getuser())
    except Exception:  # noqa: BLE001 - getuser() raises on hosts with no passwd entry or env
        os_user = None
    if os_user is not None:
        return LocalIdentity(name=os_user, source=SOURCE_OS_USER)

    return LocalIdentity(name=UNKNOWN, source=SOURCE_UNKNOWN)


def resolve_for_workspace() -> LocalIdentity:
    """`resolve()` against the current project's config, falling back cleanly if there is none."""
    try:
        from bounded_loops.workspace import discover, read_config

        return resolve(read_config(discover()))
    except Exception:  # noqa: BLE001 - no workspace, or an unreadable one, is not fatal here
        return resolve(None)
