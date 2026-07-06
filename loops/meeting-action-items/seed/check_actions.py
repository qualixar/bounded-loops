#!/usr/bin/env python3
"""
check_actions.py — a keyless "does every action item have an owner and a
due date?" gate.

Verifies that every bullet under the "## Action Items" section of a
meeting-minutes markdown document names an owner (`@name`) and a due date
(`YYYY-MM-DD`). An action item with neither is not an action item — it's a
todo that will float forever with nobody accountable and no deadline to
chase.

Pure Python standard library: no network, no API key, no external tool.

Exit code: 0 = every action item has an owner and a due date (gate
passes), 1 = one or more are missing one or both (gate fails), 2 = could
not run.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_SECTION_RE = re.compile(r"^##\s+Action Items\s*$", re.IGNORECASE | re.MULTILINE)
_NEXT_HEADING_RE = re.compile(r"^#{1,6}\s+\S", re.MULTILINE)
_BULLET_RE = re.compile(r"^\s*[-*]\s+(.+)$", re.MULTILINE)
_OWNER_RE = re.compile(r"@\w[\w.\-]*")
_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def _extract_action_section(text: str) -> str:
    m = _SECTION_RE.search(text)
    if not m:
        return ""
    rest = text[m.end():]
    next_heading = _NEXT_HEADING_RE.search(rest)
    return rest[: next_heading.start()] if next_heading else rest


def check(minutes_path: str) -> int:
    try:
        text = Path(minutes_path).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"check_actions: cannot run: {exc}", file=sys.stderr)
        return 2

    section = _extract_action_section(text)
    if not section.strip():
        print("check_actions: no '## Action Items' section found", file=sys.stderr)
        return 2

    bullets = _BULLET_RE.findall(section)
    if not bullets:
        print("check_actions: '## Action Items' section has no bullet items", file=sys.stderr)
        return 2

    violations: list[str] = []
    for bullet in bullets:
        reasons: list[str] = []
        if not _OWNER_RE.search(bullet):
            reasons.append("no owner (@name)")
        if not _DATE_RE.search(bullet):
            reasons.append("no due date (YYYY-MM-DD)")
        if reasons:
            violations.append(f"'{bullet.strip()}': {', '.join(reasons)}")

    if violations:
        print(f"check_actions: {len(violations)} action item(s) missing owner and/or due date:")
        for v in violations:
            print(f"  - {v}")
        return 1

    print("check_actions: every action item names an owner and a due date")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_actions.py <minutes.md>", file=sys.stderr)
        return 2
    return check(argv[1])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
