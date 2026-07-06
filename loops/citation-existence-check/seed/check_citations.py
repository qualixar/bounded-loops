#!/usr/bin/env python3
"""
check_citations.py — a keyless "does this case actually exist?" gate.

Verifies that every case citation in a legal document appears in a trusted
reporter of real cases. Fabricated or mis-cited authorities — the exact
failure behind the 1,600+ documented AI legal-hallucination sanctions
(Charlotin "AI Hallucination Cases" database, mid-2026) — fail the gate.

Pure Python standard library: no network, no API key, no external tool. It
runs anywhere Python does. The reporter (seed/known_reporter.json) is the
ground truth; the DOCUMENT must conform to it, never the other way round.

A citation is VOLUME REPORTER PAGE, e.g. "347 U.S. 483". The set of valid
reporter abbreviations is derived from the reporter file itself, so a
citation that uses a known reporter but a fabricated volume/page (e.g.
"599 U.S. 1201" when no such case exists) is still caught — that is the
most common hallucination shape.

Exit code: 0 = every citation is real (gate passes), 1 = one or more
citations are not in the reporter (gate fails), 2 = could not run.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def _normalize(s: str) -> str:
    """Collapse internal whitespace so '347  U.S.  483' == '347 U.S. 483'."""
    return " ".join(s.split())


def _load_reporter(path: str) -> tuple[set[str], set[str]]:
    """Return (set of known citation strings, set of reporter abbreviations)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    known: set[str] = set()
    abbrevs: set[str] = set()
    for entry in data:
        citation = _normalize(str(entry["citation"]))
        known.add(citation)
        parts = citation.split(" ")
        if len(parts) >= 3:
            abbrevs.add(" ".join(parts[1:-1]))  # everything between vol and page
    return known, abbrevs


def check(doc_path: str, reporter_path: str) -> int:
    try:
        known, abbrevs = _load_reporter(reporter_path)
        text = Path(doc_path).read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        print(f"check_citations: cannot run: {exc}", file=sys.stderr)
        return 2

    if not abbrevs:
        print("check_citations: reporter has no usable citations", file=sys.stderr)
        return 2

    # Match VOLUME <known-abbrev> PAGE. Longer abbrevs first so multi-word
    # reporters ("Cal. 4th") win over any prefix.
    abbr_alt = "|".join(re.escape(a) for a in sorted(abbrevs, key=len, reverse=True))
    cite_re = re.compile(rf"\b\d+\s+(?:{abbr_alt})\s+\d+\b")

    violations: list[str] = []
    for raw in cite_re.findall(text):
        citation = _normalize(raw)
        if citation not in known:
            violations.append(citation)

    if violations:
        print(f"check_citations: {len(violations)} citation(s) not found in the reporter:")
        for v in violations:
            print(f"  - {v}  (no such case in known_reporter.json)")
        return 1

    print("check_citations: every citation resolves to a real case in the reporter")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: check_citations.py <document> <known_reporter.json>", file=sys.stderr)
        return 2
    return check(argv[1], argv[2])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
