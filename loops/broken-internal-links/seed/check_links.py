#!/usr/bin/env python3
"""
check_links.py — a keyless "do these internal links resolve?" gate.

Verifies that every relative markdown link target inside a content
directory points at a file that actually exists on disk. Broken internal
links are one of the most common, most embarrassing content-QA failures:
a reader clicks "read the introduction" and hits a 404.

Pure Python standard library: no network, no API key, no external tool.
It runs anywhere Python does. Only RELATIVE link targets are checked
(e.g. `](./b.md)` or `](b.md)`); absolute URLs (`http://`, `https://`,
`//`) and in-page anchors (`#section`) are intentionally out of scope —
this gate is about internal file links, not external ones.

Exit code: 0 = every relative link resolves, 1 = one or more relative
links are broken, 2 = could not run.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_LINK_RE = re.compile(r"\]\(([^)\s]+)\)")
# Raw HTML anchors, which every markdown renderer passes through verbatim.
# Checking only `](target)` meant <a href="missing.md"> resolved to nothing and
# was never checked — a broken link the gate could not see.
_HTML_LINK_RE = re.compile(r"""<a\b[^>]*?\bhref\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


def _is_relative_target(target: str) -> bool:
    """True for a link this gate should check (relative file path)."""
    if target.startswith(("http://", "https://", "//", "mailto:")):
        return False
    if target.startswith("#"):
        return False
    return True


def _strip_anchor(target: str) -> str:
    """Drop any trailing '#fragment' so 'b.md#section' checks 'b.md'."""
    return target.split("#", 1)[0]


def check(content_dir: str) -> int:
    root = Path(content_dir)
    if not root.is_dir():
        print(f"check_links: not a directory: {content_dir}", file=sys.stderr)
        return 2

    md_files = sorted(root.rglob("*.md"))
    if not md_files:
        print(f"check_links: no markdown files found under {content_dir}", file=sys.stderr)
        return 2

    violations: list[str] = []
    links_examined = 0
    for md_file in md_files:
        try:
            text = md_file.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"check_links: cannot read {md_file}: {exc}", file=sys.stderr)
            return 2

        for raw_target in _LINK_RE.findall(text) + _HTML_LINK_RE.findall(text):
            if not _is_relative_target(raw_target):
                continue
            target = _strip_anchor(raw_target)
            if not target:
                continue
            links_examined += 1
            resolved = (md_file.parent / target).resolve()
            if not resolved.is_file():
                violations.append(f"{md_file.relative_to(root.parent)} -> {raw_target}")

    # "Every relative internal link resolves" is satisfied by a document containing no links, so
    # emptying, blanking, truncating or overwriting the content all passed this gate. Fixing the
    # broken links is the task; deleting them is not. Found by the held-out mutant corpus, which
    # got eight false accepts out of this one loop.
    if links_examined == 0:
        print(
            "check_links: no relative internal links found in any markdown file. A document with "
            "no links satisfies 'every link resolves' vacuously — repair the links, do not "
            "remove them."
        )
        return 1

    if violations:
        print(f"check_links: {len(violations)} broken internal link(s):")
        for v in violations:
            print(f"  - {v}  (target does not exist on disk)")
        return 1

    print("check_links: every relative internal link resolves to a real file")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_links.py <content-dir>", file=sys.stderr)
        return 2
    return check(argv[1])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
