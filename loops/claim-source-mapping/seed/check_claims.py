#!/usr/bin/env python3
"""
check_claims.py — a keyless "does this citation actually exist?" gate.

Verifies that every inline citation marker `[S#]` used in an article body
resolves to a real source id in a trusted source list. A mis-numbered or
fabricated reference — citing `[S3]` when no source S3 exists — is the exact
failure this checker catches: the article must conform to the source list,
never the other way round.

Pure Python standard library: no network, no API key, no external tool.

Exit code: 0 = every citation marker resolves to a real source id (gate
passes), 1 = one or more citation markers are not in the source list (gate
fails), 2 = could not run.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_CITE_RE = re.compile(r"\[S(\d+)\]")


def _load_source_ids(path: str) -> set[str]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {str(entry["id"]) for entry in data}


def check(article_path: str, sources_path: str) -> int:
    try:
        known_ids = _load_source_ids(sources_path)
        text = Path(article_path).read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        print(f"check_claims: cannot run: {exc}", file=sys.stderr)
        return 2

    if not known_ids:
        print("check_claims: source list has no usable source ids", file=sys.stderr)
        return 2

    cited = sorted({f"S{n}" for n in _CITE_RE.findall(text)}, key=lambda s: int(s[1:]))
    violations = [cite for cite in cited if cite not in known_ids]

    if violations:
        print(f"check_claims: {len(violations)} citation(s) not found in sources.json:")
        for v in violations:
            print(f"  - [{v}]  (no such source id in sources.json)")
        return 1

    print("check_claims: every citation resolves to a real source in sources.json")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: check_claims.py <article.md> <sources.json>", file=sys.stderr)
        return 2
    return check(argv[1], argv[2])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
