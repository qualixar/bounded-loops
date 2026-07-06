#!/usr/bin/env python3
"""
check_idoc.py — a keyless "is this IDoc structurally valid?" gate.

Verifies the shape of a simplified SAP IDoc (Intermediate Document) XML
file: a root <IDOC> containing a control-record segment <EDI_DC40> with
its three mandatory sub-fields (MANDT, DOCNUM, MESTYP), a header segment
<E1EDK01>, and at least one item segment <E1EDP01> carrying a non-empty
<MENGE> (quantity) child. This is the minimum an SAP inbound-IDoc
processing function module (e.g. IDOC_INPUT_ORDERS) needs to accept the
document instead of rejecting it into status 51 (application error).

Pure Python standard library (xml.etree.ElementTree): no network, no SAP
RFC connection, no key. It runs anywhere Python does.

Exit code: 0 = structurally valid, 1 = one or more required elements
missing (gate fails, lists exactly what's missing), 2 = could not run
(file missing / not well-formed XML).
"""
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

_CONTROL_FIELDS = ("MANDT", "DOCNUM", "MESTYP")


def check(idoc_path: str) -> int:
    path = Path(idoc_path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"check_idoc: cannot read {idoc_path}: {exc}", file=sys.stderr)
        return 2

    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        print(f"check_idoc: {idoc_path} is not well-formed XML: {exc}", file=sys.stderr)
        return 2

    missing: list[str] = []

    if root.tag != "IDOC":
        missing.append("root element <IDOC>")
        print("check_idoc: missing " + "; ".join(missing), file=sys.stderr)
        return 1

    control = root.find("EDI_DC40")
    if control is None:
        missing.append("control segment <EDI_DC40>")
    else:
        for field in _CONTROL_FIELDS:
            node = control.find(field)
            if node is None or not (node.text or "").strip():
                missing.append(f"<EDI_DC40>/<{field}>")

    header = root.find("E1EDK01")
    if header is None:
        missing.append("header segment <E1EDK01>")

    items = root.findall("E1EDP01")
    if not items:
        missing.append("at least one item segment <E1EDP01>")
    else:
        if not any((item.find("MENGE") is not None and (item.find("MENGE").text or "").strip())
                   for item in items):
            missing.append("<E1EDP01>/<MENGE> (quantity) non-empty on at least one item")

    if missing:
        print(f"check_idoc: {len(missing)} required element(s) missing or empty:")
        for m in missing:
            print(f"  - {m}")
        return 1

    print("check_idoc: IDoc structure is valid — control segment, header, and item quantity present")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_idoc.py <idoc.xml>", file=sys.stderr)
        return 2
    return check(argv[1])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
