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

A citation is VOLUME REPORTER PAGE, e.g. "347 U.S. 483". Two passes:

  1. A citation using a reporter the trusted file KNOWS is checked against the
     real volume/page — so "599 U.S. 1201", where no such case exists, is
     caught. That is the most common hallucination shape.
  2. A citation using a reporter the trusted file does NOT know is reported as
     UNVERIFIABLE.

Pass 2 exists because pass 1 alone had a hole big enough to drive the entire
threat model through. The reporter pattern used to be built FROM the reporter
file, so a citation in any other reporter never matched the regex and was never
examined: "Smith v. Jones, 500 F.3d 100" — a wholly invented case — exited 0.
The gate was not being lenient, it was blind, and inventing a reporter is
strictly easier than inventing a page number in a real one.

An unverifiable citation FAILS. A gate that cannot check something must not
pass it: silence is not a clean bill of health. Statutes, regulations and
procedural rules share the citation shape but are not cases and are excluded
explicitly — see `_NON_CASE_SOURCES`.

Exit code: 0 = every citation is real (gate passes), 1 = one or more
citations are not in the reporter (gate fails), 2 = could not run.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


#: A reporter abbreviation token — "U.", "S.", "F.", "Cal.", or a series marker
#: like "2d" / "3d" / "4th".
#: An abbreviation ending in a period ("U.", "F.", "Cal."), a bare uppercase abbreviation
#: ("WL", "LEXIS" — Westlaw and LEXIS carry no periods, and a fabricated cite in one was
#: invisible for the same reason F.3d was), or an ordinal series marker ("2d", "3d").
_REPORTER_TOKEN = r"(?:[A-Z][A-Za-z]*\.|[A-Z]{2,}|\d(?:d|st|nd|rd|th))"

#: VOLUME <any reporter-shaped abbreviation> PAGE. Deliberately NOT built from
#: the reporter file: this is the pattern that finds citations the trusted
#: reporter has never heard of.
_ANY_CITATION_RE = re.compile(
    # Page numbers run past four digits in the regional and commercial reporters
    # ("500 WL 12345"); a cap of four silently un-matched every one of them.
    rf"\b\d{{1,5}}\s+((?:{_REPORTER_TOKEN}\s*){{1,4}})\s*\d{{1,6}}\b"
)

#: Statutes, regulations and procedural rules cite in the same
#: VOLUME SOURCE PAGE shape as cases but are not cases, so they are not
#: verifiable against a case reporter and must not be flagged. This gate checks
#: CASE citations; these sources are explicitly out of scope. The list is
#: deliberately explicit rather than a loose heuristic — extend it rather than
#: widening the pattern.
_NON_CASE_SOURCES = frozenset({
    "u.s.c.", "u.s.c.a.", "u.s.c.s.", "c.f.r.", "stat.", "fed. reg.",
    "fed. r. civ. p.", "fed. r. crim. p.", "fed. r. evid.", "f. r. civ. p.",
})


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

    # Pass 1 — VOLUME <known-abbrev> PAGE. Longer abbrevs first so multi-word
    # reporters ("Cal. 4th") win over any prefix.
    abbr_alt = "|".join(re.escape(a) for a in sorted(abbrevs, key=len, reverse=True))
    cite_re = re.compile(rf"\b\d+\s+(?:{abbr_alt})\s+\d+\b")

    violations: list[str] = []
    for raw in cite_re.findall(text):
        citation = _normalize(raw)
        if citation not in known:
            violations.append((citation, "no such case in known_reporter.json"))

    # Pass 2 — citations in a reporter the trusted file does not contain.
    known_abbrevs = {abbrev.lower() for abbrev in abbrevs}
    for match in _ANY_CITATION_RE.finditer(text):
        reporter = _normalize(match.group(1)).lower()
        if reporter in known_abbrevs or reporter in _NON_CASE_SOURCES:
            continue
        violations.append((
            _normalize(match.group(0)),
            f"reporter {reporter!r} is not in known_reporter.json — "
            "this citation cannot be verified against any real case",
        ))

    # "Every citation resolves to a real case" is satisfied by a brief containing no citations, so
    # emptying, blanking, truncating or overwriting the document all passed. Replacing an invented
    # citation with a real one is the task; deleting the citations is not. Found by the held-out
    # mutant corpus.
    citations_seen = len(cite_re.findall(text)) + sum(1 for _ in _ANY_CITATION_RE.finditer(text))
    if citations_seen == 0:
        print(
            "check_citations: no citations found. A document that cites nothing satisfies "
            "'every citation resolves' vacuously — correct the citations, do not remove them."
        )
        return 1

    if violations:
        print(f"check_citations: {len(violations)} unverified citation(s):")
        for citation, reason in violations:
            print(f"  - {citation}  ({reason})")
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
