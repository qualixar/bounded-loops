#!/usr/bin/env python3
"""
check_dockerfile.py — a keyless "container doesn't run as root, doesn't
float on :latest" gate.

Pure Python standard library: no network, no API key, no external tool (no
hadolint/trivy). Requires a `USER` instruction whose value is not `root` or
`0`, and forbids a base image (`FROM`) tagged `:latest` or left untagged
(no tag at all is equivalent to `:latest`).

Exit code: 0 = both checks pass (gate passes), 1 = one or more violations
(gate fails), 2 = could not run.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_FROM_RE = re.compile(r"(?im)^\s*FROM\s+(\S+)(?:\s+AS\s+\S+)?\s*$")
_USER_RE = re.compile(r"(?im)^\s*USER\s+(\S+)\s*$")


def _base_image_violation(image_ref: str) -> str | None:
    """Return a violation message if the image ref floats on :latest."""
    if "@" in image_ref:
        # Digest-pinned images are exempt from the tag check.
        return None
    if ":" not in image_ref:
        return f"base image '{image_ref}' has no tag (implicit :latest) — pin an explicit version tag"
    tag = image_ref.rsplit(":", 1)[-1]
    if tag == "latest":
        return f"base image '{image_ref}' is tagged :latest — pin an explicit version tag"
    return None


def check(path: str) -> int:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"check_dockerfile: cannot run: {exc}", file=sys.stderr)
        return 2

    violations: list[str] = []

    from_matches = _FROM_RE.findall(text)
    if not from_matches:
        print("check_dockerfile: no FROM instruction found", file=sys.stderr)
        return 2
    for image_ref in from_matches:
        v = _base_image_violation(image_ref)
        if v:
            violations.append(v)

    user_matches = _USER_RE.findall(text)
    non_root_users = [u for u in user_matches if u.lower() not in ("root", "0")]
    if not non_root_users:
        violations.append("no USER instruction switching to a non-root user found")

    if violations:
        print(f"check_dockerfile: {len(violations)} violation(s) found:")
        for v in violations:
            print(f"  - {v}")
        return 1

    print("check_dockerfile: base image is pinned and a non-root USER is set")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_dockerfile.py <Dockerfile>", file=sys.stderr)
        return 2
    return check(argv[1])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
