#!/usr/bin/env python3
"""
check_secrets.py — a keyless "no hardcoded secrets" gate.

Pure Python standard library: no network, no API key, no external tool
(no gitleaks/trivy). Scans a source file for three hardcoded-secret shapes:

  1. An AWS access key id: AKIA[0-9A-Z]{16}
  2. A private-key header: -----BEGIN ... PRIVATE KEY-----
  3. A password/api_key/secret assignment with a non-empty string literal,
     e.g. `password = "..."`, `api_key = '...'`, `secret = "..."`.

Exit code: 0 = no hardcoded secrets found (gate passes), 1 = one or more
findings (gate fails), 2 = could not run.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_AWS_KEY_RE = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_ASSIGNMENT_RE = re.compile(
    r"(?im)^\s*(?:[A-Za-z_][A-Za-z0-9_]*\.)?"
    r"(password|api_key|secret)\s*=\s*"
    r"""(['"])(?P<value>[^'"]+)\2\s*(?:#.*)?$"""
)


def check(path: str) -> int:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"check_secrets: cannot run: {exc}", file=sys.stderr)
        return 2

    findings: list[str] = []

    for match in _AWS_KEY_RE.finditer(text):
        findings.append(f"hardcoded AWS access key id: {match.group(0)}")

    if _PRIVATE_KEY_RE.search(text):
        findings.append("hardcoded private key header found (-----BEGIN ... PRIVATE KEY-----)")

    for line in text.splitlines():
        m = _ASSIGNMENT_RE.match(line)
        if m and m.group("value").strip():
            findings.append(f"hardcoded {m.group(1)} literal: {line.strip()}")

    if findings:
        print(f"check_secrets: {len(findings)} hardcoded secret(s) found:")
        for f in findings:
            print(f"  - {f}")
        return 1

    print("check_secrets: no hardcoded secrets found")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_secrets.py <source_file>", file=sys.stderr)
        return 2
    return check(argv[1])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
