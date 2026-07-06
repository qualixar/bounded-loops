#!/usr/bin/env python3
"""
check_meta.py — a keyless "does this SEO metadata fit search-result limits?" gate.

Verifies that a page's `title` and `description` metadata are both
non-empty and within the character limits search engines actually
truncate at: ~60 characters for a title, ~160 characters for a
description. Metadata that overflows these limits gets cut off with an
ellipsis in real search results — a well-documented, widely-cited SEO
failure.

Pure Python standard library: no network, no API key, no external tool.
It runs anywhere Python does.

Exit code: 0 = both fields non-empty and within limits (gate passes), 1 =
one or more fields violate a limit (gate fails), 2 = could not run.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

MAX_TITLE_CHARS = 60
MAX_DESCRIPTION_CHARS = 160


def check(meta_path: str) -> int:
    try:
        data = json.loads(Path(meta_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"check_meta: cannot run: {exc}", file=sys.stderr)
        return 2

    if not isinstance(data, dict):
        print(f"check_meta: {meta_path} must contain a JSON object", file=sys.stderr)
        return 2

    title = data.get("title")
    description = data.get("description")
    if not isinstance(title, str) or not isinstance(description, str):
        print("check_meta: 'title' and 'description' must both be strings", file=sys.stderr)
        return 2

    violations: list[str] = []

    if not title.strip():
        violations.append("title is empty")
    elif len(title) > MAX_TITLE_CHARS:
        violations.append(f"title is {len(title)} chars, exceeds {MAX_TITLE_CHARS} limit")

    if not description.strip():
        violations.append("description is empty")
    elif len(description) > MAX_DESCRIPTION_CHARS:
        violations.append(
            f"description is {len(description)} chars, exceeds {MAX_DESCRIPTION_CHARS} limit"
        )

    if violations:
        print(f"check_meta: {len(violations)} violation(s):")
        for v in violations:
            print(f"  - {v}")
        return 1

    print(
        f"check_meta: title ({len(title)} chars) and description "
        f"({len(description)} chars) are both within limits"
    )
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_meta.py <meta.json>", file=sys.stderr)
        return 2
    return check(argv[1])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
