#!/usr/bin/env python3
"""
check_commits.py — a keyless "are these commit subjects Conventional
Commits-compliant?" gate.

Verifies that every line of a commit-subject list matches the Conventional
Commits format: `type(optional-scope): description`. A malformed subject
breaks changelog generation and semantic-release tooling that parses commit
history to decide the next version bump — the failure this gate exists to
catch.

Pure Python standard library: no network, no API key, no external tool. It
runs anywhere Python does.

Allowed types: feat, fix, docs, style, refactor, perf, test, build, ci, chore,
revert — the type list of Conventional Commits v1.0.0.
Scope, if present, is lowercase alphanumeric plus hyphens, in parentheses.
An optional `!` before the colon marks a breaking change (`feat!:`,
`feat(api)!:`), which the spec defines and which this gate must accept: it is
the single most important subject a release tool reads, and rejecting it made
this gate refuse valid input it advertised support for.
A description must follow the colon-space.

Exit code: 0 = every line conforms (gate passes), 1 = one or more lines
violate the format (gate fails), 2 = could not run.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_SUBJECT_RE = re.compile(
    r"^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)"
    r"(\([a-z0-9-]+\))?"
    r"!?"  # breaking-change marker, per Conventional Commits v1.0.0
    r": .+$"
)


def check(commits_path: str) -> int:
    try:
        text = Path(commits_path).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"check_commits: cannot run: {exc}", file=sys.stderr)
        return 2

    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        print("check_commits: no commit subjects found", file=sys.stderr)
        return 2

    violations: list[tuple[int, str]] = []
    for lineno, line in enumerate(lines, start=1):
        if not _SUBJECT_RE.match(line):
            violations.append((lineno, line))

    if violations:
        print(f"check_commits: {len(violations)} malformed subject(s):")
        for lineno, line in violations:
            print(f"  - line {lineno}: {line!r}")
        return 1

    print(f"check_commits: all {len(lines)} commit subject(s) conform")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_commits.py <commits.txt>", file=sys.stderr)
        return 2
    return check(argv[1])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
