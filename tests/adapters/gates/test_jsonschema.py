"""
Acceptance tests for JsonSchemaGate.

Validates ctx.workspace / "output.json" against a JSON Schema file.
Pure file-read + schema validation, no subprocess. GateError is reserved
for construction-time failures (missing/malformed schema file); every
runtime outcome against output.json is a Verdict, never an exception.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bounded_loops.adapters.gates.jsonschema import JsonSchemaGate
from bounded_loops.domain.errors import GateError
from bounded_loops.domain.models import LoopContext, Rung, Verdict

_NAME_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}},
    "required": ["name"],
}


def _ctx(workspace) -> LoopContext:
    return LoopContext(
        workspace=workspace,
        lap=1,
        rung=Rung.L1,
        trace_id="trace-jsonschema-1",
        env={},
    )


def _write_schema(tmp_path, schema: dict) -> Path:
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(json.dumps(schema), encoding="utf-8")
    return schema_path


def test_valid_json_matching_schema_passes(tmp_path):
    schema_path = _write_schema(tmp_path, _NAME_SCHEMA)
    (tmp_path / "output.json").write_text(json.dumps({"name": "x"}), encoding="utf-8")

    gate = JsonSchemaGate(schema_path)
    result = gate.check(_ctx(tmp_path))

    assert isinstance(result, Verdict)
    assert result.passed is True


def test_json_violating_schema_fails_not_exception(tmp_path):
    schema_path = _write_schema(tmp_path, _NAME_SCHEMA)
    (tmp_path / "output.json").write_text(json.dumps({"name": 123}), encoding="utf-8")

    gate = JsonSchemaGate(schema_path)
    result = gate.check(_ctx(tmp_path))

    assert isinstance(result, Verdict)
    assert result.passed is False


def test_missing_output_json_fails(tmp_path):
    schema_path = _write_schema(tmp_path, _NAME_SCHEMA)

    gate = JsonSchemaGate(schema_path)
    result = gate.check(_ctx(tmp_path))

    assert isinstance(result, Verdict)
    assert result.passed is False
    assert "not found" in result.detail


def test_malformed_json_fails(tmp_path):
    schema_path = _write_schema(tmp_path, _NAME_SCHEMA)
    (tmp_path / "output.json").write_text("{not valid json", encoding="utf-8")

    gate = JsonSchemaGate(schema_path)
    result = gate.check(_ctx(tmp_path))

    assert isinstance(result, Verdict)
    assert result.passed is False
    assert "not valid JSON" in result.detail


def test_missing_schema_file_raises_gate_error_at_construction(tmp_path):
    missing_schema = tmp_path / "does_not_exist_schema.json"

    with pytest.raises(GateError):
        JsonSchemaGate(missing_schema)


def test_malformed_schema_file_raises_gate_error_at_construction(tmp_path):
    # The constructor previously had no try/except around
    # `json.loads(schema_path.read_text(...))`, so a malformed schema file
    # propagated a raw json.JSONDecodeError instead of GateError —
    # inconsistent with the missing-file case just above (already
    # GateError) and with the module's own stated GateError philosophy.
    schema_path = tmp_path / "bad_schema.json"
    schema_path.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(GateError):
        JsonSchemaGate(schema_path)
