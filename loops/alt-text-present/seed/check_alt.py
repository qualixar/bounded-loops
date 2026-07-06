#!/usr/bin/env python3
"""
check_alt.py — a keyless "does every image have alt text?" gate.

Verifies that every markdown image `![alt](src)` in a post has non-empty
alt text. Missing alt text is a well-documented, widely-cited
accessibility and SEO failure: screen-reader users get nothing, and image
search has no text to index.

Pure Python standard library: no network, no API key, no external tool.
It runs anywhere Python does.

Exit code: 0 = every image has non-empty alt text, 1 = one or more images
are missing alt text, 2 = could not run.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# ![alt-text](src) — alt-text group may be empty, which is the failure case.
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")


def check(post_path: str) -> int:
    try:
        text = Path(post_path).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"check_alt: cannot run: {exc}", file=sys.stderr)
        return 2

    images = _IMAGE_RE.findall(text)
    if not images:
        print(f"check_alt: no markdown images found in {post_path}", file=sys.stderr)
        return 2

    violations: list[str] = []
    for alt, src in images:
        if not alt.strip():
            violations.append(src)

    if violations:
        print(f"check_alt: {len(violations)} image(s) missing alt text:")
        for src in violations:
            print(f"  - {src}  (empty alt text)")
        return 1

    print("check_alt: every image has non-empty alt text")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_alt.py <post.md>", file=sys.stderr)
        return 2
    return check(argv[1])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
