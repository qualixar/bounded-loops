#!/usr/bin/env python3
"""
check_alt.py — a keyless "does every image have alt text?" gate.

Verifies that every image in a post has non-empty alt text. Missing alt text
is a well-documented, widely-cited accessibility and SEO failure:
screen-reader users get nothing, and image search has no text to index.

BOTH image syntaxes markdown accepts are checked:
  - markdown  `![alt](src)`
  - raw HTML  `<img src="..." alt="...">`, which every markdown renderer passes
    through verbatim

Checking only the markdown form left the simplest possible evasion wide open:
`<img src="chart.png">` — an image with no alt attribute at all — passed this
gate silently, because the regex could not see it. That is not a gate being
lenient, it is a gate being blind, and an image the checker cannot see is the
one shape it most needs to catch.

A MISSING `alt` and an EMPTY `alt` are both violations here. (The sibling
`a11y` loop deliberately permits `alt=""` for decorative images under WCAG;
this gate's contract is the stricter "every image carries description".)

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
# <img ...> in any casing; the attribute soup is parsed separately.
_HTML_IMG_RE = re.compile(r"<img\b([^>]*?)/?>", re.IGNORECASE)
_HTML_ATTR_RE = re.compile(
    r"""([A-Za-z_:][-\w:.]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))"""
)


def _html_attrs(raw: str) -> dict[str, str]:
    """Attribute map for one <img> tag's inner text, names lowercased."""
    attrs: dict[str, str] = {}
    for match in _HTML_ATTR_RE.finditer(raw):
        double, single, bare = match.group(2), match.group(3), match.group(4)
        value = double if double is not None else (single if single is not None else bare)
        attrs[match.group(1).lower()] = value or ""
    return attrs


def _images(text: str) -> list[tuple[str | None, str]]:
    """Every image in the post as ``(alt_or_None, src)``. ``None`` = no alt at all."""
    found: list[tuple[str | None, str]] = [
        (alt, src) for alt, src in _IMAGE_RE.findall(text)
    ]
    for raw in _HTML_IMG_RE.findall(text):
        attrs = _html_attrs(raw)
        found.append((attrs.get("alt"), attrs.get("src", "<no src>")))
    return found


def check(post_path: str) -> int:
    try:
        text = Path(post_path).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"check_alt: cannot run: {exc}", file=sys.stderr)
        return 2

    images = _images(text)
    if not images:
        print(f"check_alt: no images found in {post_path}", file=sys.stderr)
        return 2

    violations: list[tuple[str, str]] = []
    for alt, src in images:
        if alt is None:
            violations.append((src, "no alt attribute"))
        elif not alt.strip():
            violations.append((src, "empty alt text"))

    if violations:
        print(f"check_alt: {len(violations)} image(s) missing alt text:")
        for src, reason in violations:
            print(f"  - {src}  ({reason})")
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
