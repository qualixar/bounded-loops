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

A heading alone is NOT a section. Every required section must also have
CONTENT beneath it — anything before the next heading at the same or a higher
level. Checking headings only meant a runbook consisting of nothing but the
seven required headings and no text whatsoever passed this gate, which is the
precise failure the module docstring says it exists to prevent: the on-call
engineer opens Rollback at 3am and finds an empty heading. A gate that cannot
tell a written runbook from a table of contents is not checking completeness.

Sub-headings count as content, so a Mitigation section organised as
"### Step 1 / ### Step 2" is complete, not empty.

Exit code: 0 = every required section present and non-empty (gate passes),
1 = one or more sections missing or empty (gate fails), 2 = could not run.
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

_HEADING_RE = re.compile(r"^\s*(#{1,6})\s+(.+?)\s*$")


def _sections(text: str) -> dict[str, str]:
    """Map lowercased heading text -> the content written beneath it.

    A section runs until the next heading at the SAME OR A HIGHER level, so a
    sub-heading and its prose belong to the parent section instead of ending it.
    That is what lets "## Mitigation / ### Step 1 / ..." count as written.
    """
    sections: dict[str, str] = {}
    open_sections: list[tuple[str, int, list[str]]] = []

    def close_to(level: int) -> None:
        while open_sections and open_sections[-1][1] >= level:
            name, _, body = open_sections.pop()
            sections[name] = "\n".join(body).strip()

    for line in text.splitlines():
        match = _HEADING_RE.match(line)
        if match is None:
            for _, _, body in open_sections:
                body.append(line)
            continue
        level = len(match.group(1))
        close_to(level)
        for _, _, body in open_sections:
            body.append(line)  # a sub-heading is content for its parent
        open_sections.append((match.group(2).strip().lower(), level, []))

    close_to(1)
    return sections


def check(runbook_path: str) -> int:
    try:
        text = Path(runbook_path).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"check_runbook: cannot run: {exc}", file=sys.stderr)
        return 2

    sections = _sections(text)
    missing = [s for s in REQUIRED_SECTIONS if s.lower() not in sections]
    empty = [s for s in REQUIRED_SECTIONS if s.lower() in sections and not sections[s.lower()]]

    if missing or empty:
        print(
            f"check_runbook: {len(missing)} required section(s) missing, "
            f"{len(empty)} present but empty:"
        )
        for s in missing:
            print(f"  - {s}  (no such heading)")
        for s in empty:
            print(f"  - {s}  (heading present, but nothing is written under it)")
        return 1

    print("check_runbook: all required sections present and non-empty")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_runbook.py <runbook.md>", file=sys.stderr)
        return 2
    return check(argv[1])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
