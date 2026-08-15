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


def check(path: str) -> int:
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        print(f"check_pins: cannot run: {exc}", file=sys.stderr)
        return 2

    violations: list[str] = []
    declared = 0
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        declared += 1
        if not _EXACT_PIN_RE.match(line):
            violations.append(line)

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
