"""Acceptance tests for bounded_loops/trust_store.py."""
from bounded_loops.trust_store import record_trust, is_trusted


def test_untrusted_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("BOUNDED_LOOPS_TRUST_STORE", str(tmp_path / "trust.json"))
    assert is_trusted(tmp_path, "pytest -q") is False


def test_record_then_is_trusted(tmp_path, monkeypatch):
    monkeypatch.setenv("BOUNDED_LOOPS_TRUST_STORE", str(tmp_path / "trust.json"))
    record_trust(tmp_path, "pytest -q")
    assert is_trusted(tmp_path, "pytest -q") is True


def test_trust_does_not_transfer_to_a_different_command(tmp_path, monkeypatch):
    monkeypatch.setenv("BOUNDED_LOOPS_TRUST_STORE", str(tmp_path / "trust.json"))
    record_trust(tmp_path, "pytest -q")
    assert is_trusted(tmp_path, "pytest -q --different-flag") is False


def test_corrupted_store_fails_closed(tmp_path, monkeypatch):
    store = tmp_path / "trust.json"
    store.write_text("{not valid json")
    monkeypatch.setenv("BOUNDED_LOOPS_TRUST_STORE", str(store))
    assert is_trusted(tmp_path, "pytest -q") is False


def test_record_trust_creates_parent_directory(tmp_path, monkeypatch):
    """The default store path is ~/.bounded-loops/trust.json — the parent
    dir may not exist yet on a fresh machine; record_trust must create it."""
    nested = tmp_path / "nested" / "does-not-exist-yet"
    monkeypatch.setenv("BOUNDED_LOOPS_TRUST_STORE", str(nested / "trust.json"))
    record_trust(tmp_path, "pytest -q")
    assert (nested / "trust.json").exists()
    assert is_trusted(tmp_path, "pytest -q") is True


def test_trust_is_specific_to_the_exact_directory(tmp_path, monkeypatch):
    """Trust for one directory does not transfer to a different directory,
    even with the identical gate command — proves the key binds BOTH the
    resolved loop_dir and the command text, not just the command."""
    monkeypatch.setenv("BOUNDED_LOOPS_TRUST_STORE", str(tmp_path / "trust.json"))
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    record_trust(dir_a, "pytest -q")
    assert is_trusted(dir_a, "pytest -q") is True
    assert is_trusted(dir_b, "pytest -q") is False


def test_record_trust_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("BOUNDED_LOOPS_TRUST_STORE", str(tmp_path / "trust.json"))
    record_trust(tmp_path, "pytest -q")
    record_trust(tmp_path, "pytest -q")
    assert is_trusted(tmp_path, "pytest -q") is True


def test_cli_yes_flag_does_not_record_trust(tmp_path, monkeypatch):
    """Fix proof — --yes (CI bypass) is NOT a human review event and
    must never establish trust on its own.

    Invokes `bl run <loop> --yes` against a real loop (stub runner + a
    command gate that trivially passes), then asserts is_trusted(loop_dir,
    gate_cmd) is still False — --yes must never write a trust entry."""
    monkeypatch.setenv("BOUNDED_LOOPS_TRUST_STORE", str(tmp_path / "trust.json"))

    loop_dir = tmp_path / "loop"
    loop_dir.mkdir()
    (loop_dir / "PROMPT.md").write_text("do the thing\n", encoding="utf-8")
    (loop_dir / "bounds.yaml").write_text("max_iterations: 1\n", encoding="utf-8")
    (loop_dir / "loop.yaml").write_text(
        "name: yes-flag-loop\n"
        "description: trust store --yes regression test\n"
        "pattern: augmented-llm\n"
        "role: [backend]\n"
        "rung: L1\n"
        "runner:\n"
        "  default: shell\n"
        "  agent_cmd: \"true\"\n"
        "gate:\n"
        "  kind: command\n"
        "  run: \"true\"\n",
        encoding="utf-8",
    )

    from bounded_loops.cli import main

    code = main(["run", str(loop_dir), "--yes"])

    assert code == 0
    assert is_trusted(loop_dir.resolve(), "true") is False


def test_default_store_path_used_when_env_not_set(monkeypatch):
    """Without BOUNDED_LOOPS_TRUST_STORE set, the module falls back to
    ~/.bounded-loops/trust.json (verified via the internal _store_path
    helper rather than touching the real home directory)."""
    monkeypatch.delenv("BOUNDED_LOOPS_TRUST_STORE", raising=False)
    from bounded_loops.trust_store import _store_path
    from pathlib import Path

    assert _store_path() == Path.home() / ".bounded-loops" / "trust.json"
