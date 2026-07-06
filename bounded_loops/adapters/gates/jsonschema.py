"""
JsonSchemaGate — validates ctx.workspace/output.json against a JSON
Schema file.

Unlike CommandGate, this gate does no subprocess execution — it is pure
file-read + schema validation, using the `jsonschema` PyPI package (a
real third-party dependency, unlike command.py/pytest.py which are
stdlib-only).

GateError is raised ONLY at construction time, when the schema file
itself is missing (schema_path.exists() check) or malformed. Every
RUNTIME outcome against output.json
(missing file, invalid JSON, schema-validation failure) is a
Verdict(passed=False, ...) — never an exception — per 's FAIL-vs-
ERROR distinction: the gate itself CAN run (the schema is loaded and
valid), so a missing or non-conforming output.json is a normal gate
FAIL, not an execution error.

NOTE: this module is named jsonschema.py while also needing to import
the real top-level `jsonschema` PyPI package internally. Python 3's
absolute-import semantics (PEP 328, default since Python 3.0) resolve
`import jsonschema` inside this file to the top-level installed
package, not to this module itself — there is no self-import collision.
"""

from __future__ import annotations

import json
from pathlib import Path

from bounded_loops.domain.errors import GateError
from bounded_loops.domain.models import LoopContext, Verdict


class JsonSchemaGate:
    """Validates ctx.workspace/output.json against a JSON Schema file."""

    schema_path: Path

    def __init__(self, schema_path: Path) -> None:
        if not schema_path.exists():
            raise GateError(f"JsonSchemaGate: schema file not found: {schema_path}")
        self.schema_path = schema_path
        try:
            self._schema = json.loads(schema_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise GateError(
                f"JsonSchemaGate: schema file {schema_path} is not valid JSON: {exc}"
            ) from exc

    def check(self, ctx: LoopContext) -> Verdict:
        target = ctx.workspace / "output.json"
        if not target.exists():
            return Verdict(
                passed=False,
                detail="output.json not found",
                evidence={"target": str(target)},
            )

        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return Verdict(
                passed=False,
                detail=f"output.json is not valid JSON: {exc}",
                evidence={"target": str(target)},
            )

        import jsonschema as jsonschema_lib  # type: ignore[import-untyped]

        try:
            jsonschema_lib.validate(instance=data, schema=self._schema)
        except jsonschema_lib.ValidationError as exc:
            return Verdict(
                passed=False,
                detail=f"schema validation failed: {exc.message}",
                evidence={"path": list(exc.absolute_path)},
            )

        return Verdict(
            passed=True,
            detail="output.json validates against schema",
            evidence={},
        )
