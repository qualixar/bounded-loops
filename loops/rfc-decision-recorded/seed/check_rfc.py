#!/usr/bin/env python3
"""
check_rfc.py — a keyless "does this RFC actually record a decision?" gate.

Verifies that an RFC markdown document has all four sections a decision
record needs to be useful later: Status, Context, Decision, Consequences.
An RFC that stops at "Context" without ever stating what was decided, or
what the tradeoffs of that decision were, is not a decision record — it's
a problem statement nobody can act on or audit after the fact.

Pure Python standard library: no network, no API key, no external tool.
Heading matching is case-insensitive (an RFC author may write "## decision"
or "## DECISION"; both count).

Exit code: 0 = all four sections present (gate passes), 1 = one or more
sections are missing (gate fails), 2 = could not run.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_REQUIRED_SECTIONS = ("status", "context", "decision", "consequences")
_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)


def _found_headings(text: str) -> set[str]:
    return {m.group(1).strip().lower() for m in _HEADING_RE.finditer(text)}


def check(rfc_path: str) -> int:
    try:
        text = Path(rfc_path).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"check_rfc: cannot run: {exc}", file=sys.stderr)
        return 2

    headings = _found_headings(text)
    missing = [s for s in _REQUIRED_SECTIONS if s not in headings]

    if missing:
        print(f"check_rfc: {len(missing)} required section(s) missing:")
        for m in missing:
            print(f"  - {m.title()}")
        return 1

    print("check_rfc: Status, Context, Decision, and Consequences are all present")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_rfc.py <rfc.md>", file=sys.stderr)
        return 2
    return check(argv[1])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
