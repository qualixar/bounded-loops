#!/usr/bin/env python3
"""
check_pins.py — a keyless "every dependency is pinned to an exact version" gate.

Pure Python standard library: no network, no API key, no external tool.
Every non-comment, non-blank line of a requirements.txt-style file must pin
an exact version with `==`. Bare names, or ranges using `>=`/`<=`/`~=`/`>`/
`<`/`!=`, are rejected as unpinned — an unpinned dependency can silently
pull in a new, unreviewed, possibly-compromised release.

ACCEPTED alongside the plain `name==version` form, because PEP 440 and the
requirements-file format allow them and every one of these IS exactly pinned:
  - extras:               `requests[security]==2.31.0`
  - space around `==`:    `urllib3 == 2.0.7`
  - local versions:       `torch==2.1.0+cpu`
  - epochs:               `foo==1!2.0`
  - environment markers:  `tomli==2.0.1; python_version < "3.11"`
Rejecting those was this gate blocking correct work — a false rejection, which
costs a retry loop an attempt and teaches the agent to mangle valid input.

STILL REJECTED, and deliberately: `foo==1.0.*`. A wildcard is a prefix match,
not an exact pin, and it is precisely the shape this gate exists to catch.

Exit code: 0 = every dependency is exactly pinned (gate passes), 1 = one or
more unpinned dependencies (gate fails), 2 = could not run.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_EXACT_PIN_RE = re.compile(
    r"^[A-Za-z0-9_.\-]+"                 # distribution name
    r"(?:\[[A-Za-z0-9_.,\-\s]+\])?"      # optional extras: requests[security]
    r"\s*==\s*"                          # the exact-pin operator, space permitted
    r"[A-Za-z0-9_.\-+!]+"                # version: local (+), epoch (!). NO '*'.
    r"\s*(?:;.*)?$"                      # optional environment marker
)


#: The distributions this file shipped with. PROMPT.md: "Do not delete a dependency to make it
#: pass — every dependency must remain, just pinned to an exact version."
#:
#: The vacuity guard below catches an EMPTIED file. It does not catch a file with one dependency
#: removed, which is the cheaper and more likely evasion: delete the unpinned `numpy` line and
#: every remaining dependency is pinned. Found by a held-out mutant authored from the stated
#: purpose, one round after the emptied-file case was fixed — the same defect, one step subtler.
#:
#: Names, not lines: the VERSIONS are supposed to change, that is the task.
SEEDED_PACKAGES = frozenset({"requests", "flask", "pydantic", "numpy"})

#: The version constraint each dependency shipped with. A pin has to SATISFY it: `flask>=2.3.0`
#: pinned to `flask==1.0.0` is not the dependency the file declared, it is a downgrade wearing an
#: exact pin.
#:
#: One half of "use a concrete, currently-real release version": the pin must not contradict the
#: file's own floor. The other half — does that release EXIST — is answered by
#: `known_releases.json`, keylessly, the same way `citation-existence-check` answers it.
#:
#: A loop that must track releases live rather than from shipped data composes this format gate
#: with a network-backed one; `loop.yaml` supports `composite` for exactly that. The reference file
#: is what makes the keyless demo honest, not a substitute for a resolver in production.
SEEDED_CONSTRAINTS: dict[str, tuple[str, str]] = {
    "requests": (">=", "2.31.0"),
    "flask": (">=", "2.3.0"),
    "pydantic": (">=", "2.5.0"),
}


def _version_tuple(version: str) -> tuple[int, ...]:
    """Numeric release segment of a PEP 440 version, for ordering. Non-numeric parts are dropped."""
    head = re.split(r"[+!]", version)[0]
    parts: list[int] = []
    for segment in head.split("."):
        digits = re.match(r"\d+", segment)
        if digits is None:
            break
        parts.append(int(digits.group(0)))
    return tuple(parts)


def _known_releases(checker_dir: Path) -> dict[str, set[str]]:
    """Real released versions per package, from the loop's shipped reference data.

    "Use a concrete, currently-real release version" was first filed as unverifiable without an
    index. That was wrong, and wrong in an instructive way: the conclusion came from the SHAPE of
    the requirement rather than from looking at what the catalog already does. `citation-existence-
    check` answers the identical question — does this thing exist — keylessly, by shipping
    `known_reporter.json` and checking against it.

    A FLOOR, never a ceiling. A package in the file must pin a version in the file; a package
    absent from it is not checked at all. That asymmetry is what stops a reference list from
    rejecting a real release it has not heard of, which would be a false reject on correct work.
    """
    try:
        document = json.loads((checker_dir / "known_releases.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    releases = document.get("releases", {})
    return {
        str(name).lower(): {str(v) for v in versions}
        for name, versions in releases.items()
        if isinstance(versions, list)
    }


def _violates_seeded_constraint(name: str, pinned: str) -> str | None:
    """Why this pin contradicts the constraint the file shipped with, or None."""
    constraint = SEEDED_CONSTRAINTS.get(name)
    if constraint is None:
        return None
    operator, floor = constraint
    if operator == ">=" and _version_tuple(pinned) < _version_tuple(floor):
        return f"{name}=={pinned} is below the {name}{operator}{floor} the file declared"
    return None

_NAME_RE = re.compile(r"^([A-Za-z0-9_.\-]+)")


def _distribution(line: str) -> str:
    """The distribution name a requirement line declares, lowercased."""
    match = _NAME_RE.match(line)
    return match.group(1).lower() if match else ""


#: This check belongs to the LOOP, not to the general checker, and the two are the same file.
#: `check_pins.py` is a reusable validator that the 0.6.2 regression pins hand small synthetic
#: inputs; the "nothing was deleted" claim is about the one artifact this loop asks a worker to
#: repair. Applying it to every input made two of those pins fail as false rejects — a fix trading
#: a false accept for a false reject, which is the exact regression the pins exist to catch.
#:
#: Scoped by the artifact's own location, `seed/<name>`, which is where the loop keeps it and
#: nowhere a caller passing an arbitrary file would land.
def _is_the_loops_own_artifact(path: Path, name: str) -> bool:
    return path.name == name and path.parent.name == "seed"


def check(path: str) -> int:
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        print(f"check_pins: cannot run: {exc}", file=sys.stderr)
        return 2

    violations: list[str] = []
    present: set[str] = set()
    declared = 0
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        declared += 1
        present.add(_distribution(line))
        if not _EXACT_PIN_RE.match(line):
            violations.append(line)

    removed = sorted(SEEDED_PACKAGES - present)
    if removed and _is_the_loops_own_artifact(Path(path), "requirements.txt"):
        print(
            f"check_pins: {removed} no longer appear(s) in the file. Every dependency must "
            "remain, pinned to an exact version — removing one is not pinning it."
        )
        return 1

    # A requirements file with no dependencies in it has no UNPINNED dependencies either, so the
    # violation list is empty and the gate used to pass. "Every dependency is pinned" is satisfied
    # vacuously by deleting the dependencies, which is the opposite of what the loop asks for.
    # Found by the held-out mutant corpus: emptying, blanking or truncating this file all passed.
    if declared == 0:
        print(
            "check_pins: no dependencies found. An empty requirements file satisfies "
            "'every dependency is pinned' only vacuously — pin the dependencies, do not remove them."
        )
        return 1

    known = _known_releases(Path(__file__).parent)
    downgrades: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "==" not in line:
            continue
        name, _, rest = line.partition("==")
        distribution = _distribution(name)
        version = rest.strip().split(";")[0].strip()

        problem = _violates_seeded_constraint(distribution, version)
        if problem:
            downgrades.append(problem)
            continue

        releases = known.get(distribution)
        if releases and version not in releases:
            downgrades.append(
                f"{distribution}=={version} is not a released version of {distribution}. "
                f"An exact pin to a version that was never published is not a pin to anything."
            )

    if downgrades:
        print(f"check_pins: {len(downgrades)} pin(s) contradict the file's own constraint:")
        for problem in downgrades:
            print(f"  - {problem}")
        return 1

    if violations:
        print(f"check_pins: {len(violations)} unpinned dependenc{'y' if len(violations) == 1 else 'ies'}:")
        for v in violations:
            print(f"  - {v}  (must pin an exact version with ==)")
        return 1

    print("check_pins: every dependency is pinned to an exact version")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_pins.py <requirements_file>", file=sys.stderr)
        return 2
    return check(argv[1])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
