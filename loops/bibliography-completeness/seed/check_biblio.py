#!/usr/bin/env python3
"""
check_biblio.py — a keyless "does this citation key actually appear in the
bibliography?" gate.

Verifies that every pandoc-style inline citation `[@key]` used in a paper's
body appears as a listed key under its `## References` section. A citation
that references a key not present in the bibliography — the "cited but never
listed" hallucination shape — fails the gate: the paper body must conform to
its own References section, never the other way round.

Pure Python standard library: no network, no API key, no external tool.

Exit code: 0 = every cited key is listed in References (gate passes),
1 = one or more cited keys are missing from References (gate fails),
2 = could not run.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_CITE_RE = re.compile(r"\[@([A-Za-z0-9_:-]+)\]")
_REF_KEY_RE = re.compile(r"^-\s*([A-Za-z0-9_:-]+):", re.MULTILINE)


def _split_references(text: str) -> tuple[str, str]:
    """Return (body, references_section) split at the '## References' header."""
    marker = "## References"
    idx = text.find(marker)
    if idx == -1:
        return text, ""
    return text[:idx], text[idx + len(marker) :]


def check(paper_path: str) -> int:
    try:
        text = Path(paper_path).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"check_biblio: cannot run: {exc}", file=sys.stderr)
        return 2

    body, references = _split_references(text)
    if not references:
        print("check_biblio: no '## References' section found", file=sys.stderr)
        return 2

    cited = sorted(set(_CITE_RE.findall(body)))
    listed = set(_REF_KEY_RE.findall(references))

    if not listed:
        print("check_biblio: References section has no usable keys", file=sys.stderr)
        return 2

    violations = [key for key in cited if key not in listed]

    if violations:
        print(f"check_biblio: {len(violations)} citation key(s) not found in References:")
        for v in violations:
            print(f"  - [@{v}]  (no such key in the '## References' section)")
        return 1

    print("check_biblio: every cited key appears in References")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_biblio.py <paper.md>", file=sys.stderr)
        return 2
    return check(argv[1])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
