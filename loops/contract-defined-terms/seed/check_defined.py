#!/usr/bin/env python3
"""
check_defined.py — a keyless "is every bolded defined term actually
defined?" gate.

Verifies that every capitalized term the contract body bolds with
markdown `**Term**` (e.g. a defined term used in a sentence) is actually
defined in the contract's "## Definitions" section. A term used in the
body but never defined is a common drafting defect — it leaves the reader
(or a court) guessing at the parties' intent, and is exactly the kind of
gap an AI-drafted contract introduces when it reuses a defined-term style
without keeping the definitions list in sync.

Pure Python standard library: no network, no API key, no external tool.
It runs anywhere Python does.

Definitions section entries are recognized in either of two common
drafting styles:
  - **"Term"** means ...   (bolded, quoted)
  - **Term** means ...     (bolded, unquoted)

Body usages are every `**Term**` occurrence OUTSIDE the Definitions
section. A term is flagged if it is bolded in the body but does not
appear (case-sensitively, exact term text) in the set of terms defined
under Definitions.

Exit code: 0 = every bolded body term is defined (gate passes),
1 = one or more bolded body terms are used but not defined (gate fails),
2 = could not run.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_BOLD_RE = re.compile(r"\*\*\"?([^*\"]+?)\"?\*\*")
_DEFINITIONS_HEADING_RE = re.compile(r"^##\s+Definitions\s*$", re.IGNORECASE | re.MULTILINE)
_NEXT_HEADING_RE = re.compile(r"^##\s+", re.MULTILINE)


def _split_definitions_section(text: str) -> tuple[str, str]:
    """Return (definitions_section_text, rest_of_document_text).

    If no '## Definitions' heading is found, definitions_section_text is
    empty and the whole document is treated as body.
    """
    match = _DEFINITIONS_HEADING_RE.search(text)
    if match is None:
        return "", text

    start = match.end()
    rest = text[start:]
    next_heading = _NEXT_HEADING_RE.search(rest)
    if next_heading is None:
        definitions_text = rest
        after_text = ""
    else:
        definitions_text = rest[: next_heading.start()]
        after_text = rest[next_heading.start() :]

    body_text = text[: match.start()] + after_text
    return definitions_text, body_text


def check(doc_path: str) -> int:
    try:
        text = Path(doc_path).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"check_defined: cannot run: {exc}", file=sys.stderr)
        return 2

    definitions_text, body_text = _split_definitions_section(text)

    defined_terms: set[str] = {m.strip() for m in _BOLD_RE.findall(definitions_text)}
    used_terms = [m.strip() for m in _BOLD_RE.findall(body_text)]

    if not defined_terms:
        print("check_defined: no '## Definitions' section with bolded terms found", file=sys.stderr)
        return 2

    seen: list[str] = []
    undefined: list[str] = []
    for term in used_terms:
        if term in defined_terms:
            continue
        if term not in seen:
            seen.append(term)
            undefined.append(term)

    if undefined:
        print(f"check_defined: {len(undefined)} bolded term(s) used but not defined:")
        for term in undefined:
            print(f"  - {term}")
        return 1

    print("check_defined: every bolded term used in the body is defined")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_defined.py <contract.md>", file=sys.stderr)
        return 2
    return check(argv[1])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
