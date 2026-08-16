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

#: Phrases that VOID the obligation a clause names, matched in the clause BODY.
#:
#: Heading-only checking was the whole gate, and a held-out mutant authored from PROMPT.md walked
#: through it three ways: `## Confidentiality` over "Neither party shall be required to hold the
#: other party's Confidential Information in confidence"; `## Governing Law` over "shall not be
#: governed by the laws of any specific jurisdiction ... expressly disclaim any designated forum";
#: and a Return of Materials section permitting the recipient to keep everything. PROMPT.md says
#: "Do not delete or weaken any existing term", and each of these weakens one to nothing while
#: leaving the heading the gate reads untouched.
#:
#: **A bare negation scan cannot be used here, and that is the whole difficulty.** A correct
#: confidentiality clause says "shall not disclose"; a correct sub-processor clause says "shall not
#: engage ... without prior authorization". Negation is the normal grammar of an obligation. So
#: these are DISCLAIMERS — phrases that release a party from a duty rather than impose one — and
#: each was checked against the converged documents before being added. Getting this wrong trades
#: a false accept for a false reject, which is what the 0.6.2 "Permitted Disclosures" regression
#: did to three legitimate NDAs.
_DISCLAIMERS = re.compile(
    r"(?:"
    r"shall not be required to"
    r"|neither party shall be required"
    r"|is not required to"
    r"|shall have no obligation"
    r"|(?:no|without any) (?:obligation|commitment|duty|liability|responsibility)"
    r"|deems appropriate in its sole discretion"
    r"|shall not remain liable"
    r"|expressly disclaim"
    r"|shall not be governed by"
    r"|free to use and disclose"
    r"|without restriction"
    r"|at its sole discretion without"
    r"|may retain (?:and use )?(?:all|any) (?:such )?(?:confidential )?(?:information|materials)"
    r")",
    re.IGNORECASE,
)


def _sections(text: str) -> list[tuple[str, str]]:
    """`(normalized heading, body)` for every markdown section, in document order."""
    matches = list(_HEADING_RE.finditer(text))
    found: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        found.append((_normalize(match.group(1)), text[match.end():end]))
    return found


def _voided(text: str) -> list[str]:
    """Sections whose body releases the duty their heading declares."""
    return [
        heading
        for heading, body in _sections(text)
        if _DISCLAIMERS.search(body)
    ]


def _has_written_content(body: str) -> bool:
    """Whether a section body says anything, as opposed to being a heading with nothing under it.

    Sub-headings alone are not content: a section organised as `### TBD` and nothing else is a
    table of contents one level down, which is the same defect the heading check exists to prevent.
    """
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.lstrip("-*+0123456789. ").strip():
            return True
    return False


def _hollow(text: str, required: tuple) -> list[str]:
    """Required items declared as a heading whose body is empty.

    Added in 0.6.5. The post-freeze corpus (E7) kept every heading and emptied every body, and this
    gate passed: not missing, because the headings were present, and not voided, because an empty
    body contains no disclaimer. `runbook-completeness` had already been repaired for exactly this
    shape in 0.6.2 (defect M2); this gate and two siblings were left with it. A document whose
    required headings are present and whose bodies are empty is worthless, and a gate that cannot
    tell it from a real one is not checking completeness.
    """
    sections = _sections(text)
    hollow: list[str] = []
    for label, phrases in required:
        bodies = [
            body for heading, body in sections
            if any(re.search(rf"\b{re.escape(phrase)}(?:e?s)?\b", heading) for phrase in phrases)
            and not _is_negated(heading)
        ]
        if bodies and not any(_has_written_content(body) for body in bodies):
            hollow.append(label)
    return hollow


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

    voided = _voided(text)
    if voided:
        print(f"check_dpa: {len(voided)} term(s) present in heading but voided in body:")
        for heading in voided:
            print(f"  - '{heading}' releases the obligation it declares")
        return 1

    if missing:
        print(f"check_dpa: {len(missing)} mandatory Art.28(3) term(s) missing:")
        for label in missing:
            print(f"  - {label}")
        return 1

    hollow = _hollow(text, MANDATORY_TERMS)
    if hollow:
        print(f"check_dpa: {len(hollow)} term(s) declared as a heading with an empty body:")
        for label in hollow:
            print(f"  - {label!r} has a heading and no text beneath it")
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
