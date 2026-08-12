"""Tests for bounded_loops.graph.cli_graph — RED-first TDD.

Full round-trip coverage: lint, plan, demo, status, artifacts, run.
Each test calls the handler function directly (no subprocess) so a future
cli.py registration snippet does not need to be present for the test suite
to run.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

# This import will fail (ImportError / ModuleNotFoundError) until
# bounded_loops/graph/cli_graph.py is created — that's the RED state.
from bounded_loops.graph.cli_graph import (
    DEMO_CONNECTIONS_LIST,
    DEMO_MANIFEST_YAML,
    cmd_graph_artifacts,
    cmd_graph_demo,
    cmd_graph_lint,
    cmd_graph_plan,
    cmd_graph_run,
    cmd_graph_status,
    register,
)


# ── helper ─────────────────────────────────────────────────────────────────────

def _ns(**kw: object) -> argparse.Namespace:
    """Build a minimal Namespace with json=False by default."""
    kw.setdefault("json", False)
    return argparse.Namespace(**kw)


# ── lint ───────────────────────────────────────────────────────────────────────

def test_lint_valid_yaml(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    manifest = tmp_path / "graph.yaml"
    manifest.write_text(DEMO_MANIFEST_YAML, encoding="utf-8")
    rc = cmd_graph_lint(_ns(manifest=str(manifest)))
    assert rc == 0
    out = capsys.readouterr().out
    assert "sha256:" in out
    assert "OK" in out


def test_lint_valid_yaml_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    manifest = tmp_path / "graph.yaml"
    manifest.write_text(DEMO_MANIFEST_YAML, encoding="utf-8")
    rc = cmd_graph_lint(_ns(manifest=str(manifest), json=True))
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["valid"] is True
    assert data["schema_version"] == 1
    assert isinstance(data["digest"], str)
    assert data["digest"].startswith("sha256:")
    assert isinstance(data["node_ids"], list)
    assert "research" in data["node_ids"]
    assert isinstance(data["slot_ids"], list)
    assert "model" in data["slot_ids"]


def test_lint_invalid_yaml(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    manifest = tmp_path / "bad.yaml"
    manifest.write_text("api_version: wrong\ngraph_id: nope\n", encoding="utf-8")
    rc = cmd_graph_lint(_ns(manifest=str(manifest)))
    assert rc == 2
    err = capsys.readouterr().err
    assert err  # some error on stderr


def test_lint_invalid_yaml_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    manifest = tmp_path / "bad.yaml"
    manifest.write_text("api_version: wrong\n", encoding="utf-8")
    rc = cmd_graph_lint(_ns(manifest=str(manifest), json=True))
    assert rc == 2
    data = json.loads(capsys.readouterr().out)
    assert data["valid"] is False
    assert "code" in data


def test_lint_json_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """lint accepts .json extension and parses via JSON path."""
    import yaml as _yaml

    raw = _yaml.safe_load(DEMO_MANIFEST_YAML)
    manifest = tmp_path / "graph.json"
    manifest.write_text(json.dumps(raw), encoding="utf-8")
    rc = cmd_graph_lint(_ns(manifest=str(manifest)))
    assert rc == 0


def test_lint_unknown_extension(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    manifest = tmp_path / "graph.txt"
    manifest.write_text(DEMO_MANIFEST_YAML, encoding="utf-8")
    rc = cmd_graph_lint(_ns(manifest=str(manifest)))
    assert rc == 2
    assert capsys.readouterr().err


# ── plan ───────────────────────────────────────────────────────────────────────

def test_plan_with_connections(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    manifest = tmp_path / "graph.yaml"
    manifest.write_text(DEMO_MANIFEST_YAML, encoding="utf-8")
    conn_file = tmp_path / "connections.json"
    conn_file.write_text(json.dumps(DEMO_CONNECTIONS_LIST), encoding="utf-8")
    rc = cmd_graph_plan(_ns(manifest=str(manifest), connections=str(conn_file)))
    assert rc == 0
    out = capsys.readouterr().out
    assert "sha256:" in out  # plan_id printed


def test_plan_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    manifest = tmp_path / "graph.yaml"
    manifest.write_text(DEMO_MANIFEST_YAML, encoding="utf-8")
    conn_file = tmp_path / "connections.json"
    conn_file.write_text(json.dumps(DEMO_CONNECTIONS_LIST), encoding="utf-8")
    rc = cmd_graph_plan(
        _ns(manifest=str(manifest), connections=str(conn_file), json=True)
    )
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["schema_version"] == 1
    assert data["plan_id"].startswith("sha256:")
    assert isinstance(data["levels"], list)
    assert isinstance(data["nodes"], list)
    assert data["nodes"][0]["node_id"] == "research"
    assert "bindings" in data


def test_plan_without_connections(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Graph with connection slots and no --connections → exit 2, honest error."""
    manifest = tmp_path / "graph.yaml"
    manifest.write_text(DEMO_MANIFEST_YAML, encoding="utf-8")
    rc = cmd_graph_plan(_ns(manifest=str(manifest), connections=None))
    assert rc == 2
    err = capsys.readouterr().err
    assert "connection" in err.lower()


# ── demo ───────────────────────────────────────────────────────────────────────

def test_demo_creates_expected_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out_dir = tmp_path / "run-out"
    rc = cmd_graph_demo(_ns(out=str(out_dir)))
    assert rc == 0, capsys.readouterr()
    assert (out_dir / "plan.json").exists()
    assert (out_dir / "controller-events.jsonl").exists()
    assert (out_dir / "manifest.yaml").exists()
    assert (out_dir / "connections.json").exists()
    assert (out_dir / "run-meta.json").exists()
    meta_dir = out_dir / "artifacts" / "metadata"
    assert meta_dir.exists()
    assert any(meta_dir.glob("*.json"))
    captured = capsys.readouterr()
    assert "DEMONSTRATION" in captured.out
    assert "NOT executed" in captured.out


def test_demo_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out_dir = tmp_path / "run-json"
    rc = cmd_graph_demo(_ns(out=str(out_dir), json=True))
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["demonstration"] is True
    assert "run_state" in data
    assert "run_id" in data
    assert data["out"] == str(out_dir)


def test_demo_run_state_recorded(tmp_path: Path) -> None:
    out_dir = tmp_path / "run-state"
    rc = cmd_graph_demo(_ns(out=str(out_dir)))
    assert rc == 0
    lines = (out_dir / "controller-events.jsonl").read_text().splitlines()
    assert lines, "event log must not be empty"
    last = json.loads(lines[-1])
    assert last["event_type"] in ("run.succeeded", "run.failed")


def test_demo_plan_json_is_bytes_of_canonical(tmp_path: Path) -> None:
    out_dir = tmp_path / "plan-check"
    cmd_graph_demo(_ns(out=str(out_dir)))
    raw = (out_dir / "plan.json").read_bytes()
    # Must be valid JSON and contain the plan_id
    parsed = json.loads(raw)
    assert "source_graph_digest" in parsed


# ── status ─────────────────────────────────────────────────────────────────────

def test_status_after_demo(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out_dir = tmp_path / "status-run"
    cmd_graph_demo(_ns(out=str(out_dir)))
    capsys.readouterr()  # discard demo output
    rc = cmd_graph_status(_ns(run=str(out_dir)))
    assert rc == 0
    out = capsys.readouterr().out
    assert "research" in out


def test_status_json_after_demo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out_dir = tmp_path / "status-json"
    cmd_graph_demo(_ns(out=str(out_dir)))
    capsys.readouterr()
    rc = cmd_graph_status(_ns(run=str(out_dir), json=True))
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert "run_state" in data
    assert "nodes" in data


def test_status_json_is_serializable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """dataclasses.asdict of ArenaProjection must round-trip through json.dumps."""
    out_dir = tmp_path / "status-serial"
    cmd_graph_demo(_ns(out=str(out_dir)))
    capsys.readouterr()
    cmd_graph_status(_ns(run=str(out_dir), json=True))
    raw = capsys.readouterr().out
    # Must parse without error
    parsed = json.loads(raw)
    # Re-serialise with sort_keys — should not raise
    json.dumps(parsed, sort_keys=True)


# ── artifacts ──────────────────────────────────────────────────────────────────

def test_artifacts_after_demo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out_dir = tmp_path / "artifacts-run"
    cmd_graph_demo(_ns(out=str(out_dir)))
    capsys.readouterr()
    rc = cmd_graph_artifacts(_ns(run=str(out_dir)))
    assert rc == 0
    out = capsys.readouterr().out
    assert "sha256:" in out


def test_artifacts_json_after_demo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out_dir = tmp_path / "artifacts-json"
    cmd_graph_demo(_ns(out=str(out_dir)))
    capsys.readouterr()
    rc = cmd_graph_artifacts(_ns(run=str(out_dir), json=True))
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert isinstance(data, list)
    assert len(data) > 0
    assert "digest" in data[0]
    assert "media_type" in data[0]
    assert "size" in data[0]
    assert "state" in data[0]


# ── run (honest no-exec) ───────────────────────────────────────────────────────

def test_run_prints_honest_notice_and_exits_0(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = tmp_path / "graph.yaml"
    manifest.write_text(DEMO_MANIFEST_YAML, encoding="utf-8")
    conn_file = tmp_path / "connections.json"
    conn_file.write_text(json.dumps(DEMO_CONNECTIONS_LIST), encoding="utf-8")
    rc = cmd_graph_run(_ns(manifest=str(manifest), connections=str(conn_file)))
    assert rc == 0
    out = capsys.readouterr().out
    # Must contain an honest notice — a compile-only preview that points at --execute.
    combined = out.lower()
    assert "compile-only" in combined or "preview" in combined


def test_run_does_not_execute_nodes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """run must not create any artifacts or event logs — no node execution."""
    manifest = tmp_path / "graph.yaml"
    manifest.write_text(DEMO_MANIFEST_YAML, encoding="utf-8")
    conn_file = tmp_path / "connections.json"
    conn_file.write_text(json.dumps(DEMO_CONNECTIONS_LIST), encoding="utf-8")
    cmd_graph_run(_ns(manifest=str(manifest), connections=str(conn_file)))
    # run has no --out, so there should be no event logs in tmp_path
    assert not any(tmp_path.rglob("*.jsonl"))


# ── fix 6a (dual-audit reconciliation, doc-only): `--help` documents exit code 3 ──
# as PAUSED, not an error — both audits flagged that `set -e` / `$? -ne 0` CI checks
# would otherwise misread a paused (not broken) run as a failure.

def test_run_help_documents_exit_code_3_as_paused(capsys: pytest.CaptureFixture[str]) -> None:
    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="cmd")
    register(subs)
    with pytest.raises(SystemExit):
        parser.parse_args(["graph", "run", "--help"])
    out = capsys.readouterr().out
    assert "3" in out
    assert "PAUSED" in out.upper()
    assert "not an error" in out.lower()


# ── register ───────────────────────────────────────────────────────────────────

def test_register_wires_graph_subcommands() -> None:
    """register() adds 'graph' subparser with all expected subcommands."""
    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="cmd")
    register(subs)
    args = parser.parse_args(["graph", "lint", "some-file.yaml"])
    assert args.cmd == "graph"
    assert args.graph_cmd == "lint"
    assert hasattr(args, "func")


def test_register_demo_subparser() -> None:
    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="cmd")
    register(subs)
    args = parser.parse_args(["graph", "demo", "--out", "/tmp/x"])
    assert args.graph_cmd == "demo"
    assert hasattr(args, "func")


# ── full round-trip ─────────────────────────────────────────────────────────────

def test_full_demo_status_artifacts_round_trip(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """demo → status → artifacts all succeed on the same directory."""
    out_dir = tmp_path / "round-trip"

    # demo
    rc = cmd_graph_demo(_ns(out=str(out_dir)))
    assert rc == 0, capsys.readouterr()

    # status
    capsys.readouterr()
    rc = cmd_graph_status(_ns(run=str(out_dir)))
    assert rc == 0

    # artifacts
    capsys.readouterr()
    rc = cmd_graph_artifacts(_ns(run=str(out_dir)))
    assert rc == 0
    out = capsys.readouterr().out
    assert "ACTIVE" in out


# ── C-048 RED tests ─────────────────────────────────────────────────────────────


def test_demo_run_meta_has_demonstration_flag(tmp_path: Path) -> None:
    """run-meta.json must carry demonstration=true after cmd_graph_demo."""
    out_dir = tmp_path / "demo-meta-flag"
    rc = cmd_graph_demo(_ns(out=str(out_dir)))
    assert rc == 0
    meta = json.loads((out_dir / "run-meta.json").read_text(encoding="utf-8"))
    assert meta.get("demonstration") is True


def test_demo_json_includes_notice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--json output from demo must include a 'notice' disclaimer key."""
    out_dir = tmp_path / "demo-json-notice"
    rc = cmd_graph_demo(_ns(out=str(out_dir), json=True))
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert "notice" in data
    assert isinstance(data["notice"], str)
    assert len(data["notice"]) > 20


def test_demo_exits_2_on_failed_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """demo must return exit 2 when the run projection state is not SUCCEEDED."""
    # After ARCH-05, demo helpers live in cli_graph_demo; patch there so the
    # monkeypatch affects the name the running code actually looks up.
    import bounded_loops.graph.cli_graph_demo as _m

    class _RejectGate:
        def evaluate(  # type: ignore[override]
            self, *, plan: object, node: object, result: object
        ) -> object:
            return _m.GateVerdict(False, "test: forced rejection")

    monkeypatch.setattr(_m, "_DemoGate", _RejectGate)
    out_dir = tmp_path / "demo-fail"
    rc = cmd_graph_demo(_ns(out=str(out_dir)))
    assert rc == 2


def test_status_human_notice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Human status output must include a LOCAL/UNVERIFIED notice."""
    out_dir = tmp_path / "status-notice"
    cmd_graph_demo(_ns(out=str(out_dir)))
    capsys.readouterr()
    rc = cmd_graph_status(_ns(run=str(out_dir)))
    assert rc == 0
    out = capsys.readouterr().out
    combined = out.upper()
    assert "LOCAL" in combined or "UNVERIFIED" in combined


def test_status_json_notice_and_verified(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """JSON status must include 'notice' string and 'verified': false."""
    out_dir = tmp_path / "status-json-notice"
    cmd_graph_demo(_ns(out=str(out_dir)))
    capsys.readouterr()
    rc = cmd_graph_status(_ns(run=str(out_dir), json=True))
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert "notice" in data
    assert isinstance(data["notice"], str)
    assert data.get("verified") is False


def test_status_json_demonstration_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """JSON status must surface 'demonstration' flag from run-meta.json."""
    out_dir = tmp_path / "status-json-demo"
    cmd_graph_demo(_ns(out=str(out_dir)))
    capsys.readouterr()
    rc = cmd_graph_status(_ns(run=str(out_dir), json=True))
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert "demonstration" in data
    assert data["demonstration"] is True


def test_artifacts_exits_2_on_corrupt_metadata(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """artifacts must exit 2 and emit stderr when a metadata file is corrupt JSON."""
    out_dir = tmp_path / "artifacts-corrupt"
    cmd_graph_demo(_ns(out=str(out_dir)))
    capsys.readouterr()
    meta_dir = out_dir / "artifacts" / "metadata"
    # Inject corrupt file with a valid sha256-shaped name.
    (meta_dir / ("a" * 64 + ".json")).write_text("{not valid json", encoding="utf-8")
    rc = cmd_graph_artifacts(_ns(run=str(out_dir)))
    assert rc == 2
    assert capsys.readouterr().err


def test_run_json_shape(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """run --json must emit schema_version, plan_id, source_graph_digest, levels, nodes, notice."""
    manifest = tmp_path / "graph.yaml"
    manifest.write_text(DEMO_MANIFEST_YAML, encoding="utf-8")
    conn_file = tmp_path / "connections.json"
    conn_file.write_text(json.dumps(DEMO_CONNECTIONS_LIST), encoding="utf-8")
    rc = cmd_graph_run(
        _ns(manifest=str(manifest), connections=str(conn_file), json=True)
    )
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["schema_version"] == 1
    assert data["plan_id"].startswith("sha256:")
    assert data["source_graph_digest"].startswith("sha256:")
    assert isinstance(data["levels"], list)
    assert isinstance(data["nodes"], list)
    assert "notice" in data
    assert "preview" in data["notice"]


def test_status_exits_2_on_malformed_run_meta(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """status must exit 2 + clear stderr on truncated / missing-key run-meta.json."""
    out_dir = tmp_path / "status-malformed"
    cmd_graph_demo(_ns(out=str(out_dir)))
    capsys.readouterr()
    # Overwrite run-meta.json with truncated JSON.
    (out_dir / "run-meta.json").write_text("{", encoding="utf-8")
    rc = cmd_graph_status(_ns(run=str(out_dir)))
    assert rc == 2
    assert capsys.readouterr().err


# ── TEST-13: cli_graph_artifacts.py uncovered display branches (76% → higher) ──
# Covers: no-artifacts text message, table-format display, symlinked metadata entry.

def test_artifacts_no_records_prints_no_artifacts_found(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An artifacts directory that exists but has no valid metadata files must
    print 'No artifacts found.' (text mode) and return 0.
    Covers the 'if not records: print(...)' branch at line 62."""
    # Create a minimal run directory with an empty metadata dir
    run_dir = tmp_path / "empty-run"
    meta_dir = run_dir / "artifacts" / "metadata"
    meta_dir.mkdir(parents=True)

    rc = cmd_graph_artifacts(_ns(run=str(run_dir)))
    assert rc == 0
    out = capsys.readouterr().out
    assert "No artifacts found." in out


def test_artifacts_table_format_shows_header_and_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """In text mode (no --json), artifacts with records must display a table
    with DIGEST/MEDIA_TYPE/SIZE/STATE columns. Covers lines 64-71."""
    out_dir = tmp_path / "artifacts-table"
    cmd_graph_demo(_ns(out=str(out_dir)))
    capsys.readouterr()

    # Text mode (json=False is the default)
    rc = cmd_graph_artifacts(_ns(run=str(out_dir), json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "DIGEST" in out
    assert "MEDIA_TYPE" in out
    assert "SIZE" in out
    assert "STATE" in out
    assert "sha256:" in out


def test_artifacts_skips_symlinked_metadata_entry_and_returns_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A symlinked file inside artifacts/metadata/ must be skipped and reported
    as an error, returning exit code 2. Covers the 'if path.is_symlink()' branch
    at line 42 and the errors reporting block at lines 73-76."""
    out_dir = tmp_path / "artifacts-sym"
    cmd_graph_demo(_ns(out=str(out_dir)))
    capsys.readouterr()

    meta_dir = out_dir / "artifacts" / "metadata"
    # Plant a symlink with a sha256-shaped name pointing at an unrelated file
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    link_name = "b" * 64 + ".json"
    (meta_dir / link_name).symlink_to(target)

    rc = cmd_graph_artifacts(_ns(run=str(out_dir)))
    assert rc == 2
    err = capsys.readouterr().err
    assert "symlink" in err.lower()
