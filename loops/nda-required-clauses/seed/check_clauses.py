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
tool. It runs anywhere Python does. The check is a case-insensitive
heading/keyword match against the document body — it does not judge
legal quality, only presence of the required section.

Exit code: 0 = every required clause is present (gate passes),
1 = one or more required clauses are missing (gate fails),
2 = could not run.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Each required clause is declared as (label, [alternate keyword phrases]).
# A clause is considered PRESENT if any one of its keyword phrases appears
# case-insensitively in the document, either as a heading or in body text.
REQUIRED_CLAUSES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Confidentiality", ("confidentiality",)),
    ("Term/Duration", ("term", "duration")),
    ("Governing Law", ("governing law",)),
    ("Return of Materials", ("return of materials", "return of confidential")),
    ("Permitted Disclosures", ("permitted disclosure",)),
)


def _normalize(s: str) -> str:
    """Collapse internal whitespace for reliable substring matching."""
    return " ".join(s.split()).lower()


def check(doc_path: str) -> int:
    try:
        text = Path(doc_path).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"check_clauses: cannot run: {exc}", file=sys.stderr)
        return 2

    normalized = _normalize(text)

    missing: list[str] = []
    for label, phrases in REQUIRED_CLAUSES:
        if not any(phrase in normalized for phrase in phrases):
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
