#!/usr/bin/env python3
"""
check_cds.py — a keyless "does this CDS view declare its required
annotations?" gate.

Verifies that a SAP Core Data Services (CDS) view DDL source contains
three annotations SAP best practice (and many customer ABAP guidelines)
treat as mandatory before a view is released for consumption:
  - @AccessControl.authorizationCheck — declares how the view enforces
    authorization (a view with no authorization check exposed to Fiori/
    OData is a security gap).
  - @EndUserText.label            — the human-readable label shown in
    the data dictionary / Fiori Elements UI; without it the view has no
    business-friendly description.
  - @Metadata.allowExtensions     — whether customers/partners may extend
    the view with custom fields (S/4HANA extensibility contract).

This is a simple substring presence check on the DDL text — pure Python
standard library: no network, no SAP ABAP Development Tools (ADT)
connection, no key. It runs anywhere Python does.

Exit code: 0 = all three annotations present (gate passes), 1 = one or
more missing (gate fails, lists them), 2 = could not run (file missing).
"""
from __future__ import annotations

import sys
from pathlib import Path

_REQUIRED_ANNOTATIONS = (
    "@AccessControl.authorizationCheck",
    "@EndUserText.label",
    "@Metadata.allowExtensions",
)


def check(cds_path: str) -> int:
    try:
        text = Path(cds_path).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"check_cds: cannot read {cds_path}: {exc}", file=sys.stderr)
        return 2

    missing = [a for a in _REQUIRED_ANNOTATIONS if a not in text]

    if missing:
        print(f"check_cds: {len(missing)} required annotation(s) missing:")
        for m in missing:
            print(f"  - {m}")
        return 1

    print("check_cds: all required annotations are present")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_cds.py <zcds_view.txt>", file=sys.stderr)
        return 2
    return check(argv[1])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
