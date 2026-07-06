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
It runs anywhere Python does. The check is a case-insensitive keyword
phrase match against the document body — it does not judge legal quality,
only presence of the required term.

Exit code: 0 = every mandatory term is present (gate passes),
1 = one or more mandatory terms are missing (gate fails),
2 = could not run.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Each mandatory Art.28(3) term is declared as (label, [alternate keyword
# phrases]). A term is considered PRESENT if any one of its keyword phrases
# appears case-insensitively in the document, either as a heading or in
# body text.
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


def _normalize(s: str) -> str:
    """Collapse internal whitespace for reliable substring matching."""
    return " ".join(s.split()).lower()


def check(doc_path: str) -> int:
    try:
        text = Path(doc_path).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"check_dpa: cannot run: {exc}", file=sys.stderr)
        return 2

    normalized = _normalize(text)

    missing: list[str] = []
    for label, phrases in MANDATORY_TERMS:
        if not any(phrase in normalized for phrase in phrases):
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
