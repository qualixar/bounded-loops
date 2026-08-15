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
_LEVELLED_HEADING_RE = re.compile(r"^\s*(#{1,6})\s+(.+?)\s*$")


def _sections(text: str) -> dict[str, str]:
    """Map lowercased heading text -> what is written beneath it.

    A section runs until the next heading at the same or a higher level, so a
    sub-heading and its prose belong to the parent rather than ending it.

    Checking for headings alone let an RFC consisting of four bare headings and
    no text pass — the exact document this gate exists to reject, since a
    "## Decision" with nothing under it records no decision.
    """
    sections: dict[str, str] = {}
    open_sections: list[tuple[str, int, list[str]]] = []

    def close_to(level: int) -> None:
        while open_sections and open_sections[-1][1] >= level:
            name, _, body = open_sections.pop()
            sections[name] = "\n".join(body).strip()

    for line in text.splitlines():
        match = _LEVELLED_HEADING_RE.match(line)
        if match is None:
            for _, _, body in open_sections:
                body.append(line)
            continue
        level = len(match.group(1))
        close_to(level)
        for _, _, body in open_sections:
            body.append(line)
        open_sections.append((match.group(2).strip().lower(), level, []))

    close_to(1)
    return sections


def check(rfc_path: str) -> int:
    try:
        text = Path(rfc_path).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"check_rfc: cannot run: {exc}", file=sys.stderr)
        return 2

    sections = _sections(text)
    missing = [s for s in _REQUIRED_SECTIONS if s not in sections]
    empty = [s for s in _REQUIRED_SECTIONS if s in sections and not sections[s]]

    if missing or empty:
        print(
            f"check_rfc: {len(missing)} required section(s) missing, "
            f"{len(empty)} present but empty:"
        )
        for m in missing:
            print(f"  - {m.title()}  (no such heading)")
        for m in empty:
            print(f"  - {m.title()}  (heading present, but nothing is written under it)")
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
