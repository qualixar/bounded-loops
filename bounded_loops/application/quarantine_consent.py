"""Input quarantine: what is withheld from an agent's sandbox, and who may waive it.

Bound #3, the "governed workspace" guarantee. Two halves live here because they are one
decision: the denylist of credential-bearing names that never reach a sandbox, and the
consent rule for a loop that asks to run without it.

Moved out of `composition.py` when SEC-05's additions pushed that file past this
project's 800-line cap — the repo's own layering test caught it. The seam is the right
one anyway: composition wires adapters together, while what counts as a credential is a
policy, and policy buried in a wiring file is policy nobody reviews.

## The denylist

bounded-loops invites community loop contributions, so a shared loop's `seed/` is not
fully trusted: without this, a malicious loop could plant a reader for these paths, and
a careless one could ship real credentials that then reach an agent. Excluded by NAME at
every directory level of the copy.

SEC-05 widened it. The original covered SSH, AWS, GPG and generic keys — the 2024 shape
of the problem — and omitted every credential store an agent-era toolchain actually
writes. `.npmrc` and `.pypirc` hold publish tokens, which is the capability to ship a
malicious release; `.kube/config` holds cluster credentials. Enumerated denylists rot in
one direction, so each addition below names what it protects.

## The consent rule

SEC-06. `quarantine_inputs` is a field in `bounds.yaml`, so until 0.6.6 the *author*
of a loop could turn off the credential exclusion by themselves, and the operator who
ran it was told nothing. That is the wrong party holding the switch: the author knows
what their loop needs, the operator knows what is in their home directory, and only
the second one can judge whether disabling the exclusion is safe on this machine.

bounded-loops invites community loop contributions, which makes this concrete rather
than theoretical: a shared loop shipping `quarantine_inputs: false` and a `seed/`
directory reader would have had a supported path to whatever the denylist otherwise
keeps out of the sandbox.

The rule is the one `env_passthrough` already uses, deliberately: a capability opens
only when the WORKLOAD declares it and the OPERATOR allows it, held in two different
places — a committed file and an ambient variable — so neither party can open the
channel alone. Default-closed: an unset variable authorizes nothing.

Consent is per loop name, not a global boolean. An operator who needs one
secret-scanning demo to see a planted fake key should not thereby authorize every
loop on the machine, and a variable that could only say "yes" would make them.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

from bounded_loops.domain.errors import ManifestError

#: Comma-separated loop names for which the operator permits `quarantine_inputs: false`.
QUARANTINE_OPT_OUT_ALLOW_VAR = "BOUNDED_LOOPS_QUARANTINE_OPT_OUT_ALLOW"

QUARANTINE_DENY_NAMES = frozenset({
    ".git", ".env", ".ssh", ".aws", ".gnupg", ".netrc",
    "credentials", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    # Registry publish tokens: npm, PyPI, Docker/OCI registry auth. A publish token
    # is the capability to ship a malicious release, so these rank with SSH keys.
    ".npmrc", ".pypirc", ".docker", ".dockercfg",
    # Cluster and cloud control planes. `.config` is deliberately NOT here: it holds
    # gcloud credentials under a home directory but ordinary tool config under a
    # project, and a seed is a project. Blocking it would break honest loops to
    # inconvenience a dishonest one that has better options anyway.
    ".kube", ".azure", ".gcloud", ".terraform.d",
    # Service-account key material, which is a file rather than a directory and so
    # matched none of the suffix rules either.
    "service_account.json", "serviceaccount.json", "keyfile.json",
    "gcloud_credentials.json", "application_default_credentials.json",
    # Shell history and profiles: routinely contain exported tokens.
    ".bash_history", ".zsh_history", ".netrc.gpg", ".pgpass",
})

QUARANTINE_DENY_SUFFIXES = (
    ".pem", ".key", ".p12", ".pfx",
    # Private keys and keystores that the four suffixes above did not cover.
    ".jks", ".keystore", ".ppk", ".asc", ".kdbx",
)


def quarantine_matches(name: str) -> bool:
    """Whether one seed entry is on the secret-bearing denylist.

    Matches by exact name, `.env*` prefix, or key/cert suffix — case-insensitively.
    """
    low = name.lower()
    return (
        low in QUARANTINE_DENY_NAMES
        or low.startswith(".env")
        or low.endswith(QUARANTINE_DENY_SUFFIXES)
    )


class QuarantineIgnore:
    """`shutil.copytree` ignore callback that remembers what it withheld.

    Quarantine used to be silent. That is fine when it removes a credential nobody meant
    to ship and actively harmful when it removes a file the loop needs: the author sees a
    sandbox missing a file, with nothing anywhere saying why, and the denylist is not in
    the error they get. An exclusion an operator cannot see is indistinguishable from a
    bug — and this repo's own rule is that errors are never swallowed silently.
    """

    def __init__(self) -> None:
        self.withheld: list[str] = []

    def __call__(self, dirpath: str, names: list[str]) -> set[str]:
        skip = {name for name in names if quarantine_matches(name)}
        root = Path(dirpath).name
        self.withheld.extend(sorted(f"{root}/{name}" for name in skip))
        return skip


def quarantine_ignore(_dirpath: str, names: list[str]) -> set[str]:
    """Stateless form, for callers that do not need the report."""
    return {name for name in names if quarantine_matches(name)}


def report_quarantine(withheld: list[str]) -> None:
    """Name what quarantine kept out of the sandbox, on stderr, once.

    stderr rather than the ledger: this is a fact about how the workspace was built, not
    a verdict about the work, and the ledger's rows are verdicts. It goes to the operator
    who is about to wonder where their file went.
    """
    if not withheld:
        return
    print(
        f"[bounded-loops] quarantine withheld {len(withheld)} seed "
        f"{'entry' if len(withheld) == 1 else 'entries'} from the sandbox: "
        + ", ".join(withheld)
        + ". These names can carry credentials. Set quarantine_inputs: false in "
        "bounds.yaml only if the loop genuinely needs one of them.",
        file=sys.stderr,
    )


def operator_quarantine_grants(source: Mapping[str, str] | None = None) -> frozenset[str]:
    """Loop names the operator has authorized to run without quarantine."""
    env = os.environ if source is None else source
    raw = env.get(QUARANTINE_OPT_OUT_ALLOW_VAR, "")
    return frozenset(name.strip() for name in raw.split(",") if name.strip())


def require_quarantine_consent(
    loop_dir: Path,
    quarantine_inputs: bool,
    *,
    source: Mapping[str, str] | None = None,
) -> None:
    """Refuse to build a workspace for an unquarantined loop without operator consent.

    Refuses rather than silently re-enabling quarantine. A loop that declared
    `quarantine_inputs: false` did so because it needs a file the denylist withholds —
    the secret-scanning demo with a planted fake key is the shipped example. Running it
    with quarantine forced back on would produce a loop that fails for a reason nothing
    in its output explains, which trades a security surprise for a debugging one.
    """
    if quarantine_inputs:
        return
    name = loop_dir.resolve().name
    if name in operator_quarantine_grants(source):
        return
    raise ManifestError(
        f"loop directory '{name}' declares quarantine_inputs: false, which would copy "
        f"credential-bearing files (.env*, .ssh, .aws, .npmrc, .pypirc, .kube, "
        f"*.pem/*.key, …) from its seed into the agent's sandbox. That needs the "
        f"operator's consent as well as the author's.\n"
        f"To allow it for this loop only:\n"
        f"  export {QUARANTINE_OPT_OUT_ALLOW_VAR}={name}\n"
        f"To run it safely instead, set quarantine_inputs: true in bounds.yaml."
    )
