#!/usr/bin/env python3
"""
check_privacy.py — a keyless "does this privacy policy cover the basics?" gate.

Verifies that a privacy policy contains the six sections a working policy
needs: Data We Collect, How We Use Your Data, Data Sharing, Data
Retention, Your Rights, and Contact. Missing Data Retention or Your Rights
is a common defect in AI-drafted or hastily-assembled privacy policies —
the policy explains what data is collected and shared but never says how
long it is kept or what choices the individual has over it.

Pure Python standard library: no network, no API key, no external tool.
It runs anywhere Python does. It does not judge policy quality, only whether
the policy DECLARES each required section as its own section.

WHY HEADINGS AND NOT PROSE. This used to substring-match the whole document,
so a passing mention of a word satisfied the requirement — including a
sentence denying it. A keyword search cannot distinguish a section from a
reference to one. Requiring a heading is a narrower contract and an honest
one: a policy covering every topic in unheaded prose will now FAIL, which is
the gate refusing to certify what it cannot verify.

Matching is case-insensitive and word-boundary anchored.

Exit code: 0 = every required section is present (gate passes),
1 = one or more required sections are missing (gate fails),
2 = could not run.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# Each required section is declared as (label, [alternate keyword phrases]).
# A section is PRESENT when one of its phrases appears in a markdown HEADING —
# that is, the policy gives it its own section.
REQUIRED_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Data We Collect", ("data we collect",)),
    ("How We Use Your Data", ("how we use your data", "how we use the data")),
    ("Data Sharing", ("data sharing",)),
    ("Data Retention", ("data retention",)),
    ("Your Rights", ("your rights",)),
    ("Contact", ("contact",)),
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
    """True when some heading gives this topic its own section."""
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
        print(f"check_privacy: cannot run: {exc}", file=sys.stderr)
        return 2

    headings = _headings(text)

    missing: list[str] = []
    for label, phrases in REQUIRED_SECTIONS:
        if not any(_declared(phrase, headings) for phrase in phrases):
            missing.append(label)

    if missing:
        print(f"check_privacy: {len(missing)} required section(s) missing:")
        for label in missing:
            print(f"  - {label}")
        return 1

    print("check_privacy: all required sections are present")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_privacy.py <privacy.md>", file=sys.stderr)
        return 2
    return check(argv[1])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
