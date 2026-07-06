"""
Acceptance tests for StubRunner.

The critical fix under test: cassette `actions` must ACTUALLY
mutate ctx.workspace (write_file), not merely log text. Path-traversal,
unknown-action-type, and catch-all-lap behavior are also covered.
"""

import json
import subprocess
import sys

import pytest

from bounded_loops.adapters.runners.stub import StubRunner
from bounded_loops.domain.errors import RunnerError
from bounded_loops.domain.models import LoopContext, Rung, Spec


def _spec() -> Spec:
    return Spec(
        name="demo-loop",
        goal="Demo goal",
        steps=("step one",),
        stop_condition="pytest exits 0",
    )


def _ctx(workspace, lap: int) -> LoopContext:
    return LoopContext(
        workspace=workspace,
        lap=lap,
        rung=Rung.L1,
        trace_id="trace-stub-1",
        env={},
    )


def _write_cassette(tmp_path, interactions: list[dict], loop_name: str = "demo-loop"):
    cassette_dir = tmp_path / "cassettes"
    cassette_dir.mkdir(parents=True, exist_ok=True)
    cassette_path = cassette_dir / "default.json"
    payload = {
        "version": 1,
        "loop": loop_name,
        "created": "2026-07-04",
        "description": "test cassette",
        "interactions": interactions,
    }
    cassette_path.write_text(json.dumps(payload), encoding="utf-8")
    return cassette_path


def _three_interaction_cassette(tmp_path):
    return _write_cassette(
        tmp_path,
        [
            {
                "lap": 1,
                "agent_output": "turn one",
                "actions": [{"type": "noop"}],
                "agent_claimed_done": False,
                "changed": False,
                "tokens": 10,
            },
            {
                "lap": 2,
                "agent_output": "turn two",
                "actions": [{"type": "noop"}],
                "agent_claimed_done": True,
                "changed": True,
                "tokens": 20,
            },
            {
                "lap": 3,
                "agent_output": "turn three",
                "actions": [{"type": "noop"}],
                "agent_claimed_done": False,
                "changed": False,
                "tokens": 30,
            },
        ],
    )


# --- corrupted cassette is a RunnerError, never a raw traceback --------------

def test_corrupted_cassette_raises_runner_error_not_raw_traceback(tmp_path):
    """Fix: a truncated/corrupt cassette (disk-full write, merge
    markers, non-JSON) must surface as RunnerError — the runner is the error
    boundary — not a raw JSONDecodeError escaping wire() into an unhandled
    traceback (which crashed the CLI with exit 1 and killed the MCP server)."""
    cassette_dir = tmp_path / "cassettes"
    cassette_dir.mkdir(parents=True)
    cassette_path = cassette_dir / "default.json"
    cassette_path.write_text('{"version": 1, "interac', encoding="utf-8")  # truncated
    with pytest.raises(RunnerError):
        StubRunner(cassette_path)


def test_non_object_cassette_raises_runner_error(tmp_path):
    """A cassette whose top-level JSON is valid but not an object (e.g. a bare
    array) must be a RunnerError, not an AttributeError from raw.get(...)."""
    cassette_dir = tmp_path / "cassettes"
    cassette_dir.mkdir(parents=True)
    cassette_path = cassette_dir / "default.json"
    cassette_path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(RunnerError):
        StubRunner(cassette_path)


# --- determinism -------------------------------------------------------------

def test_stub_deterministic_replay(tmp_path):
    cassette = _three_interaction_cassette(tmp_path)
    runner = StubRunner(cassette)
    ctx = _ctx(tmp_path, lap=1)

    r1 = runner.run_once(_spec(), ctx)
    r2 = runner.run_once(_spec(), ctx)

    assert r1.changed == r2.changed
    assert r1.agent_claimed_done == r2.agent_claimed_done
    assert r1.tokens == r2.tokens
    assert r1.log == r2.log


def test_stub_replay_all_interactions(tmp_path):
    cassette = _three_interaction_cassette(tmp_path)
    runner = StubRunner(cassette)

    r1 = runner.run_once(_spec(), _ctx(tmp_path, lap=1))
    r2 = runner.run_once(_spec(), _ctx(tmp_path, lap=2))
    r3 = runner.run_once(_spec(), _ctx(tmp_path, lap=3))

    assert r1.log == "turn one" and r1.tokens == 10 and r1.agent_claimed_done is False
    assert r2.log == "turn two" and r2.tokens == 20 and r2.agent_claimed_done is True
    assert r3.log == "turn three" and r3.tokens == 30 and r3.agent_claimed_done is False


def test_stub_agent_claimed_done_false(tmp_path):
    cassette = _three_interaction_cassette(tmp_path)
    runner = StubRunner(cassette)
    result = runner.run_once(_spec(), _ctx(tmp_path, lap=1))
    assert result.agent_claimed_done is False


def test_stub_agent_claimed_done_true(tmp_path):
    cassette = _three_interaction_cassette(tmp_path)
    runner = StubRunner(cassette)
    result = runner.run_once(_spec(), _ctx(tmp_path, lap=2))
    assert result.agent_claimed_done is True


# --- agent_output.txt ---------------------------------------------------------

def test_stub_writes_agent_output_to_workspace(tmp_path):
    cassette = _write_cassette(
        tmp_path,
        [
            {
                "lap": 1,
                "agent_output": "hello from stub",
                "actions": [{"type": "noop"}],
                "agent_claimed_done": False,
                "changed": False,
                "tokens": 0,
            }
        ],
    )
    runner = StubRunner(cassette)
    runner.run_once(_spec(), _ctx(tmp_path, lap=1))
    assert (tmp_path / "agent_output.txt").read_text(encoding="utf-8") == "hello from stub"


# --- THE critical fix: actions actually mutate the workspace --------

def test_stub_applies_write_file_action_making_gate_actually_pass(tmp_path):
    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    buggy = (
        "def slugify(text: str) -> str:\n"
        "    return text.lower().replace(\" \", \"-\")\n"
    )
    (seed_dir / "slugify.py").write_text(buggy, encoding="utf-8")

    fixed = (
        "import re\n\n\n"
        "def slugify(text: str) -> str:\n"
        "    \"\"\"Convert *text* to a URL-safe slug.\"\"\"\n"
        "    text = text.lower()\n"
        "    text = re.sub(r\"[^a-z0-9\\s-]\", \"\", text)\n"
        "    text = re.sub(r\"\\s+\", \"-\", text)\n"
        "    return text.strip(\"-\")\n"
    )
    test_file = (
        "import sys\n"
        "sys.path.insert(0, 'seed')\n"
        "from slugify import slugify\n\n\n"
        "def test_multiple_spaces():\n"
        "    assert slugify('a   b') == 'a-b'\n"
    )
    (seed_dir / "test_slugify.py").write_text(test_file, encoding="utf-8")

    cassette = _write_cassette(
        tmp_path,
        [
            {
                "lap": 1,
                "agent_output": "fixing slugify",
                "actions": [
                    {"type": "write_file", "path": "seed/slugify.py", "content": fixed}
                ],
                "agent_claimed_done": True,
                "changed": True,
                "tokens": 287,
            }
        ],
    )
    runner = StubRunner(cassette)
    runner.run_once(_spec(), _ctx(tmp_path, lap=1))

    assert (tmp_path / "seed" / "slugify.py").read_text(encoding="utf-8") == fixed

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "seed/test_slugify.py"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_stub_noop_action_does_not_touch_workspace(tmp_path):
    cassette = _write_cassette(
        tmp_path,
        [
            {
                "lap": 1,
                "agent_output": "no-op turn",
                "actions": [{"type": "noop"}],
                "agent_claimed_done": False,
                "changed": False,
                "tokens": 0,
            }
        ],
    )
    runner = StubRunner(cassette)
    before = {p.name for p in tmp_path.iterdir()}
    runner.run_once(_spec(), _ctx(tmp_path, lap=1))
    after = {p.name for p in tmp_path.iterdir()}
    assert after - before == {"agent_output.txt"}


def test_stub_write_file_path_traversal_raises_runner_error(tmp_path):
    cassette = _write_cassette(
        tmp_path,
        [
            {
                "lap": 1,
                "agent_output": "malicious turn",
                "actions": [
                    {
                        "type": "write_file",
                        "path": "../../../etc/evil",
                        "content": "x",
                    }
                ],
                "agent_claimed_done": False,
                "changed": True,
                "tokens": 0,
            }
        ],
    )
    runner = StubRunner(cassette)
    with pytest.raises(RunnerError, match="resolves outside the workspace"):
        runner.run_once(_spec(), _ctx(tmp_path, lap=1))

    assert not (tmp_path.parent.parent.parent / "etc" / "evil").exists()


def test_stub_unknown_action_type_raises_at_construction(tmp_path):
    cassette = _write_cassette(
        tmp_path,
        [
            {
                "lap": 1,
                "agent_output": "bad action",
                "actions": [{"type": "delete_everything"}],
                "agent_claimed_done": False,
                "changed": False,
                "tokens": 0,
            }
        ],
    )
    with pytest.raises(RunnerError, match="Unknown cassette action type"):
        StubRunner(cassette)


def test_stub_catchall_star_lap_used_beyond_explicit_entries(tmp_path):
    cassette = _write_cassette(
        tmp_path,
        [
            {
                "lap": 1,
                "agent_output": "explicit lap 1",
                "actions": [{"type": "noop"}],
                "agent_claimed_done": False,
                "changed": False,
                "tokens": 0,
            },
            {
                "lap": "*",
                "agent_output": "catchall",
                "actions": [{"type": "noop"}],
                "agent_claimed_done": True,
                "changed": False,
                "tokens": 0,
            },
        ],
    )
    runner = StubRunner(cassette)

    r1 = runner.run_once(_spec(), _ctx(tmp_path, lap=1))
    r2 = runner.run_once(_spec(), _ctx(tmp_path, lap=2))
    r5 = runner.run_once(_spec(), _ctx(tmp_path, lap=5))

    assert r1.log == "explicit lap 1"
    assert r2.log == "catchall"
    assert r5.log == "catchall"


def test_stub_raises_runner_error_cassette_not_found(tmp_path):
    missing = tmp_path / "cassettes" / "nope.json"
    with pytest.raises(RunnerError):
        StubRunner(missing)


def test_stub_raises_runner_error_bad_version(tmp_path):
    cassette_dir = tmp_path / "cassettes"
    cassette_dir.mkdir(parents=True, exist_ok=True)
    cassette_path = cassette_dir / "default.json"
    cassette_path.write_text(
        json.dumps(
            {
                "version": 2,
                "loop": "demo-loop",
                "created": "2026-07-04",
                "description": "bad version",
                "interactions": [
                    {
                        "lap": 1,
                        "agent_output": "x",
                        "actions": [{"type": "noop"}],
                        "agent_claimed_done": False,
                        "changed": False,
                        "tokens": 0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(RunnerError, match="version"):
        StubRunner(cassette_path)


def test_stub_raises_runner_error_lap_out_of_range(tmp_path):
    cassette = _write_cassette(
        tmp_path,
        [
            {
                "lap": 1,
                "agent_output": "one",
                "actions": [{"type": "noop"}],
                "agent_claimed_done": False,
                "changed": False,
                "tokens": 0,
            },
            {
                "lap": 2,
                "agent_output": "two",
                "actions": [{"type": "noop"}],
                "agent_claimed_done": False,
                "changed": False,
                "tokens": 0,
            },
        ],
    )
    runner = StubRunner(cassette)
    with pytest.raises(RunnerError):
        runner.run_once(_spec(), _ctx(tmp_path, lap=3))


def test_stub_raises_runner_error_broken_lap_sequence(tmp_path):
    cassette = _write_cassette(
        tmp_path,
        [
            {
                "lap": 1,
                "agent_output": "one",
                "actions": [{"type": "noop"}],
                "agent_claimed_done": False,
                "changed": False,
                "tokens": 0,
            },
            {
                "lap": 3,
                "agent_output": "skips two",
                "actions": [{"type": "noop"}],
                "agent_claimed_done": False,
                "changed": False,
                "tokens": 0,
            },
        ],
    )
    with pytest.raises(RunnerError, match="lap sequence broken"):
        StubRunner(cassette)
