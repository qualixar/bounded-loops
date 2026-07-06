#!/usr/bin/env python3
"""
check_openapi.py — a keyless "is this a minimally valid OpenAPI doc?" gate.

Verifies the structural minimum an OpenAPI 3.x document must satisfy to be
usable by any downstream tooling (codegen, docs, gateways): a top-level
`openapi` version string, `info.title`, `info.version`, and — the mismatch
this loop demonstrates — every operation object nested under `paths` must
declare a non-empty `responses` object. An operation with no `responses` is a
contract with no defined outcome: clients can't know what to expect, and most
OpenAPI tooling silently mishandles or rejects it.

Pure Python standard library: no network, no external OpenAPI validator
library, no key. Runs anywhere Python does.

Exit code: 0 = document is minimally valid (gate passes), 1 = one or more
structural requirements are violated (gate fails), 2 = could not run.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_HTTP_METHODS = (
    "get",
    "put",
    "post",
    "delete",
    "options",
    "head",
    "patch",
    "trace",
)


def check(doc_path: str) -> int:
    try:
        text = Path(doc_path).read_text(encoding="utf-8")
        doc = json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"check_openapi: cannot run: {exc}", file=sys.stderr)
        return 2

    if not isinstance(doc, dict):
        print("check_openapi: document root must be a JSON object", file=sys.stderr)
        return 2

    violations: list[str] = []

    openapi_version = doc.get("openapi")
    if not isinstance(openapi_version, str) or not openapi_version.strip():
        violations.append("top-level 'openapi' must be a non-empty version string")

    info = doc.get("info")
    if not isinstance(info, dict):
        violations.append("top-level 'info' must be an object")
        info = {}

    title = info.get("title")
    if not isinstance(title, str) or not title.strip():
        violations.append("'info.title' must be a non-empty string")

    version = info.get("version")
    if not isinstance(version, str) or not version.strip():
        violations.append("'info.version' must be a non-empty string")

    paths = doc.get("paths")
    if not isinstance(paths, dict):
        violations.append("top-level 'paths' must be an object")
        paths = {}

    for path_key, path_item in sorted(paths.items()):
        if not isinstance(path_item, dict):
            violations.append(f"path '{path_key}' must be an object")
            continue
        for method, operation in sorted(path_item.items()):
            if method not in _HTTP_METHODS:
                continue
            if not isinstance(operation, dict):
                violations.append(f"operation '{method.upper()} {path_key}' must be an object")
                continue
            responses = operation.get("responses")
            if not isinstance(responses, dict) or not responses:
                violations.append(
                    f"operation '{method.upper()} {path_key}' is missing a "
                    "non-empty 'responses' object"
                )

    if violations:
        print(f"check_openapi: {len(violations)} violation(s):")
        for v in violations:
            print(f"  - {v}")
        return 1

    print("check_openapi: document satisfies the minimal OpenAPI 3 contract")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_openapi.py <openapi.json>", file=sys.stderr)
        return 2
    return check(argv[1])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
