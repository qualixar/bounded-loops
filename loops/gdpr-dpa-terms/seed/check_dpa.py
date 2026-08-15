#!/usr/bin/env python3
"""
check_dpa.py — a keyless "does this DPA cover Art.28(3) mandatory terms?" gate.

Verifies that a Data Processing Agreement contains the nine categories of
mandatory terms required by GDPR Article 28(3) for a controller-processor
relationship: subject matter, duration, nature and purpose, type of
personal data, obligations of the controller, sub-processor terms,
confidentiality, security measures, and audit rights. A DPA missing any
one of these is a common defect in AI-drafted or template-derived
agreements and is a direct compliance gap under GDPR.

Pure Python standard library: no network, no API key, no external tool.
It runs anywhere Python does. It does not judge legal quality, only whether
the agreement DECLARES each required term as its own provision.

WHY HEADINGS AND NOT PROSE. This used to substring-match the whole document
body, which meant a DPA containing the sentence "this agreement grants NO
audit rights whatsoever" satisfied the Art.28(3) audit requirement — the word
was present, so the term was "covered". A keyword search cannot tell a
provision from its negation, so it was passing agreements that failed the
exact compliance check it advertises. Requiring a heading is a narrower
contract, and an honest one: it verifies a dedicated provision exists.

The consequence is deliberate and worth stating — a DPA that covers every
Art.28(3) term in unheaded prose will now FAIL. That is this gate refusing to
certify what it cannot actually verify, which is the same discipline the rest
of this project applies to unmeasurable quantities.

Matching is case-insensitive and word-boundary anchored, so a "Termination"
heading does not satisfy a "term" requirement.

Exit code: 0 = every mandatory term is declared (gate passes),
1 = one or more mandatory terms are missing (gate fails),
2 = could not run.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# Each mandatory Art.28(3) term is declared as (label, [alternate keyword
# phrases]). A term is PRESENT when one of its phrases appears in a markdown
# HEADING — that is, the agreement gives it its own provision.
MANDATORY_TERMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Subject Matter", ("subject matter",)),
    ("Duration", ("duration",)),
    ("Nature and Purpose", ("nature and purpose",)),
    ("Type of Personal Data", ("type of personal data",)),
    ("Obligations of the Controller", ("obligations of the controller",)),
    ("Sub-Processor", ("sub-processor", "subprocessor", "sub processor")),
    ("Confidentiality", ("confidentiality",)),
    ("Security Measures", ("security measures",)),
    ("Audit", ("audit",)),
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
    """True when some heading gives this term its own provision."""
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
        print(f"check_dpa: cannot run: {exc}", file=sys.stderr)
        return 2

    headings = _headings(text)

    missing: list[str] = []
    for label, phrases in MANDATORY_TERMS:
        if not any(_declared(phrase, headings) for phrase in phrases):
            missing.append(label)

    if missing:
        print(f"check_dpa: {len(missing)} mandatory Art.28(3) term(s) missing:")
        for label in missing:
            print(f"  - {label}")
        return 1

    print("check_dpa: all mandatory Art.28(3) terms are present")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_dpa.py <dpa.md>", file=sys.stderr)
        return 2
    return check(argv[1])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
