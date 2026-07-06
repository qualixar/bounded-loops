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
It runs anywhere Python does. The check is a case-insensitive
heading/keyword match against the document body — it does not judge
legal quality, only presence of the required section.

Exit code: 0 = every required section is present (gate passes),
1 = one or more required sections are missing (gate fails),
2 = could not run.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Each required section is declared as (label, [alternate keyword
# phrases]). A section is considered PRESENT if any one of its keyword
# phrases appears case-insensitively in the document, either as a heading
# or in body text.
REQUIRED_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Data We Collect", ("data we collect",)),
    ("How We Use Your Data", ("how we use your data", "how we use the data")),
    ("Data Sharing", ("data sharing",)),
    ("Data Retention", ("data retention",)),
    ("Your Rights", ("your rights",)),
    ("Contact", ("contact",)),
)


def _normalize(s: str) -> str:
    """Collapse internal whitespace for reliable substring matching."""
    return " ".join(s.split()).lower()


def check(doc_path: str) -> int:
    try:
        text = Path(doc_path).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"check_privacy: cannot run: {exc}", file=sys.stderr)
        return 2

    normalized = _normalize(text)

    missing: list[str] = []
    for label, phrases in REQUIRED_SECTIONS:
        if not any(phrase in normalized for phrase in phrases):
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
