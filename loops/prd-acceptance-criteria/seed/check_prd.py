#!/usr/bin/env python3
"""
check_prd.py — a keyless "does every story have acceptance criteria?" gate.

Verifies that every `## Story:` section in a PRD markdown document contains
an "Acceptance Criteria" subsection with at least one bullet or checkbox
item. A story with no acceptance criteria is unshippable — nobody can tell
when it's done, and QA has nothing to verify against.

Pure Python standard library: no network, no API key, no external tool.

Exit code: 0 = every story has acceptance criteria (gate passes), 1 = one
or more stories are missing them (gate fails), 2 = could not run.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_STORY_RE = re.compile(r"^##\s+Story:\s*(.+)$", re.MULTILINE)
_AC_HEADING_RE = re.compile(r"^###\s+Acceptance Criteria\s*$", re.IGNORECASE | re.MULTILINE)
_BULLET_RE = re.compile(r"^\s*[-*]\s+(?:\[[ xX]\]\s*)?\S", re.MULTILINE)


def _split_sections(text: str) -> list[tuple[str, str]]:
    """Split the doc into (story_title, story_body) pairs by '## Story:' headings."""
    matches = list(_STORY_RE.finditer(text))
    sections: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections.append((m.group(1).strip(), text[start:end]))
    return sections


def _has_acceptance_criteria(body: str) -> bool:
    ac_match = _AC_HEADING_RE.search(body)
    if not ac_match:
        return False
    # Content after the AC heading, up to the next '##'/'###' heading or end.
    rest = body[ac_match.end():]
    next_heading = re.search(r"^#{2,3}\s+\S", rest, re.MULTILINE)
    ac_body = rest[: next_heading.start()] if next_heading else rest
    return bool(_BULLET_RE.search(ac_body))


def check(prd_path: str) -> int:
    try:
        text = Path(prd_path).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"check_prd: cannot run: {exc}", file=sys.stderr)
        return 2

    sections = _split_sections(text)
    if not sections:
        print("check_prd: no '## Story:' sections found", file=sys.stderr)
        return 2

    violations: list[str] = []
    for title, body in sections:
        if not _has_acceptance_criteria(body):
            violations.append(title)

    if violations:
        print(f"check_prd: {len(violations)} stor(y/ies) missing acceptance criteria:")
        for v in violations:
            print(f"  - {v}")
        return 1

    print("check_prd: every story has an Acceptance Criteria subsection with at least one item")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_prd.py <prd.md>", file=sys.stderr)
        return 2
    return check(argv[1])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
