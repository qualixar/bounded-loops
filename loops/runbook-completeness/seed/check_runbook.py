#!/usr/bin/env python3
"""
check_runbook.py — a keyless "is this runbook actually complete?" gate.

Verifies that an operations runbook contains every section an on-call
engineer needs during an incident: Summary, Severity, Detection, Diagnosis,
Mitigation, Rollback, Escalation. A runbook missing Rollback or Escalation
looks fine at a glance but leaves the on-call engineer stranded exactly when
it matters most — the failure this gate exists to catch.

Pure Python standard library: no network, no API key, no external tool. It
runs anywhere Python does.

A "section" is recognized as a markdown heading (any `#` level) whose text,
after stripping leading `#` characters and whitespace, matches a required
section name case-insensitively (e.g. "## severity", "# SEVERITY", and
"### Severity" all count).

Exit code: 0 = every required section present (gate passes), 1 = one or
more sections missing (gate fails), 2 = could not run.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REQUIRED_SECTIONS = (
    "Summary",
    "Severity",
    "Detection",
    "Diagnosis",
    "Mitigation",
    "Rollback",
    "Escalation",
)

_HEADING_RE = re.compile(r"^\s*#{1,6}\s+(.+?)\s*$")


def _headings(text: str) -> set[str]:
    """Return the set of lowercased, stripped markdown heading texts."""
    found: set[str] = set()
    for line in text.splitlines():
        match = _HEADING_RE.match(line)
        if match:
            found.add(match.group(1).strip().lower())
    return found


def check(runbook_path: str) -> int:
    try:
        text = Path(runbook_path).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"check_runbook: cannot run: {exc}", file=sys.stderr)
        return 2

    headings = _headings(text)
    missing = [s for s in REQUIRED_SECTIONS if s.lower() not in headings]

    if missing:
        print(f"check_runbook: {len(missing)} required section(s) missing:")
        for s in missing:
            print(f"  - {s}")
        return 1

    print("check_runbook: all required sections present")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_runbook.py <runbook.md>", file=sys.stderr)
        return 2
    return check(argv[1])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
