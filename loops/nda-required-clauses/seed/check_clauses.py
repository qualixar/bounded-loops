#!/usr/bin/env python3
"""
check_clauses.py — a keyless "does this NDA have every required clause?" gate.

Verifies that a Non-Disclosure Agreement contains all five clauses a
mutual NDA needs to be enforceable in practice: Confidentiality, a
Term/Duration, Governing Law, Return of Materials, and Permitted
Disclosures. Missing any one of these is a common defect in
AI-drafted or hastily-assembled NDAs — the agreement either never
expires, has no forum for disputes, or never requires the other party
to give back (or destroy) confidential materials.

Pure Python standard library: no network, no API key, no external
tool. It runs anywhere Python does. It does not judge legal quality, only
whether the agreement DECLARES each required clause as its own section.

WHY HEADINGS AND NOT PROSE. This used to substring-match the whole document,
so any sentence that merely mentioned a word satisfied the corresponding
requirement — including a sentence denying it. A keyword search cannot
distinguish a clause from a passing reference to one. Requiring a heading is a
narrower contract and an honest one. An NDA that covers every clause in
unheaded prose will now FAIL: that is the gate refusing to certify what it
cannot verify.

Matching is case-insensitive and word-boundary anchored, so a "Termination"
heading does not satisfy the "term" requirement.

Exit code: 0 = every required clause is present (gate passes),
1 = one or more required clauses are missing (gate fails),
2 = could not run.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# Each required clause is declared as (label, [alternate keyword phrases]).
# A clause is PRESENT when one of its phrases appears in a markdown HEADING —
# that is, the agreement gives it its own section.
REQUIRED_CLAUSES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Confidentiality", ("confidentiality",)),
    ("Term/Duration", ("term", "duration")),
    ("Governing Law", ("governing law",)),
    ("Return of Materials", ("return of materials", "return of confidential")),
    ("Permitted Disclosures", ("permitted disclosure",)),
)


_HEADING_RE = re.compile(r"^\s*#{1,6}\s+(.+?)\s*$", re.MULTILINE)


def _normalize(s: str) -> str:
    """Collapse internal whitespace for reliable matching."""
    return " ".join(s.split()).lower()


def _headings(text: str) -> list[str]:
    """The normalized text of every markdown heading in the document."""
    return [_normalize(m.group(1)) for m in _HEADING_RE.finditer(text)]


#: Words that DENY the provision the heading names, matched anywhere in it. Checking only
#: PREFIXES caught "## No Audit Rights" and missed "## Audit: None Granted" and
#: "## Rights (No Audit Permitted)" — the negation simply moved past the first word. Found by
#: the 0.6.2 Grok audit, one round after prefix-matching was itself the fix for the same class.
#:
#: Whole words only. "Non-Disclosure" is a real clause name, not a negation, and a substring
#: match on "non" would reject every NDA that has one.
_NEGATIONS = re.compile(
    r"\b(no|not|none|never|without|excluding|except|prohibited|denied|disclaimed)\b"
)


def _is_negated(heading: str) -> bool:
    return bool(_NEGATIONS.search(heading))

def _declared(phrase: str, headings: list[str]) -> bool:
    """True when some heading gives this clause its own section.

    Word-boundary anchored, so a "Termination" heading does not satisfy the
    "term" requirement.
    """
    # Trailing (e)s so a "Permitted Disclosures" heading satisfies the
    # "permitted disclosure" phrase. The LEADING boundary is what stops
    # "Termination" from satisfying "term"; a bare trailing boundary also rejected
    # every plural heading, which broke three legitimate documents.
    pattern = re.compile(rf"\b{re.escape(phrase)}(?:e?s)?\b")
    return any(
        pattern.search(heading) and not _is_negated(heading) for heading in headings
    )


def check(doc_path: str) -> int:
    try:
        text = Path(doc_path).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"check_clauses: cannot run: {exc}", file=sys.stderr)
        return 2

    headings = _headings(text)

    missing: list[str] = []
    for label, phrases in REQUIRED_CLAUSES:
        if not any(_declared(phrase, headings) for phrase in phrases):
            missing.append(label)

    if missing:
        print(f"check_clauses: {len(missing)} required clause(s) missing:")
        for label in missing:
            print(f"  - {label}")
        return 1

    print("check_clauses: all required clauses are present")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_clauses.py <nda.md>", file=sys.stderr)
        return 2
    return check(argv[1])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
