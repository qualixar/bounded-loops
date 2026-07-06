#!/usr/bin/env python3
"""
check_material.py — a keyless "is this material master record complete?"
gate.

Verifies that a material master record carries the fields its material
type requires before it can be released for use, mirroring SAP's own
material-type-driven field-selection logic (transaction OMS9/MM01 field
references):
  - material_type == "FERT" (finished product): requires non-empty
    base_uom, description, and division — a finished product must be
    sellable/plannable, which needs a unit of measure, a description for
    order/delivery documents, and a division for sales-area determination.
  - material_type == "ROH" (raw material): requires non-empty base_uom
    and description — a raw material is procured/consumed, not sold, so
    division is not required.

Pure Python standard library: no network, no SAP MM01/MM02 transaction,
no key. It runs anywhere Python does.

Exit code: 0 = all fields required for this material_type are present and
non-empty (gate passes), 1 = one or more required fields missing or empty
(gate fails, lists them), 2 = could not run (file missing / not valid
JSON / unrecognized material_type).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_REQUIRED_BY_TYPE = {
    "FERT": ("base_uom", "description", "division"),
    "ROH": ("base_uom", "description"),
}


def check(material_path: str) -> int:
    try:
        data = json.loads(Path(material_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"check_material: cannot run: {exc}", file=sys.stderr)
        return 2

    if not isinstance(data, dict):
        print("check_material: material record must be a JSON object", file=sys.stderr)
        return 2

    material_type = data.get("material_type")
    required = _REQUIRED_BY_TYPE.get(material_type)
    if required is None:
        print(
            f"check_material: unrecognized material_type {material_type!r} "
            f"(expected one of {sorted(_REQUIRED_BY_TYPE)})",
            file=sys.stderr,
        )
        return 2

    missing = [f for f in required if not str(data.get(f, "")).strip()]

    if missing:
        print(
            f"check_material: {len(missing)} required field(s) missing or empty "
            f"for material_type={material_type!r}:"
        )
        for m in missing:
            print(f"  - {m}")
        return 1

    print(f"check_material: all required fields present for material_type={material_type!r}")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_material.py <material.json>", file=sys.stderr)
        return 2
    return check(argv[1])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
