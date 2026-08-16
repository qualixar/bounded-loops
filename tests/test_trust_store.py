"""Acceptance tests for bounded_loops/trust_store.py."""
from __future__ import annotations

import errno
import json
import os

import pytest

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


def test_default_store_path_used_when_env_not_set(tmp_path, monkeypatch):
    """Without BOUNDED_LOOPS_TRUST_STORE set, the module falls back to
    the per-test temporary trust store (verified via the internal _store_path
    helper rather than touching the real home directory)."""
    monkeypatch.delenv("BOUNDED_LOOPS_TRUST_STORE", raising=False)
    from bounded_loops.trust_store import _store_path

    assert _store_path().is_relative_to(tmp_path)


# ── TEST-01: ownership / world-writability guard ──────────────────────────────


def test_world_writable_trust_file_is_rejected(tmp_path, monkeypatch):
    """A world-writable (mode & 0o002) trust store is refused even with a valid record.

    The guard is ``st.st_mode & 0o022`` — it fires on ANY write bit in the group
    or other positions.  0o622 sets both: other-write (0o002) and group-write
    (0o020).  0o644 is NOT caught (only read bits for group/other, no write bits).

    Mutation proof: delete the ``if not uid_ok or (st.st_mode & 0o022): return {}``
    check in _load() → this test fails.
    """
    monkeypatch.setenv("BOUNDED_LOOPS_TRUST_STORE", str(tmp_path / "trust.json"))
    record_trust(tmp_path, "pytest -q")
    # 0o622: owner r+w, group w, other w — mode & 0o022 = 0o022 ≠ 0
    os.chmod(tmp_path / "trust.json", 0o622)
    assert is_trusted(tmp_path, "pytest -q") is False, (
        "world-writable trust store must be refused (mode & 0o022 guard)"
    )


def test_group_writable_trust_file_is_rejected(tmp_path, monkeypatch):
    """A group-writable (mode 0o660) trust store is refused even with a valid record.

    Mutation proof: delete the mode guard → this test fails.
    """
    monkeypatch.setenv("BOUNDED_LOOPS_TRUST_STORE", str(tmp_path / "trust.json"))
    record_trust(tmp_path, "pytest -q")
    os.chmod(tmp_path / "trust.json", 0o660)  # group-writable: mode & 0o020 != 0
    assert is_trusted(tmp_path, "pytest -q") is False, (
        "group-writable trust store must be refused (mode & 0o022 guard)"
    )


def test_correctly_permissioned_trust_file_is_accepted(tmp_path, monkeypatch):
    """A 0o600 trust store passes the ownership and mode check.

    This is the positive control: it verifies the guard does not
    over-reject valid files — a guard that always returns {} would
    break this test.
    """
    monkeypatch.setenv("BOUNDED_LOOPS_TRUST_STORE", str(tmp_path / "trust.json"))
    record_trust(tmp_path, "pytest -q")
    # record_trust writes at 0o600 — no chmod needed
    assert is_trusted(tmp_path, "pytest -q") is True, (
        "correctly-permissioned trust store must be accepted"
    )


def test_foreign_uid_trust_file_is_rejected(tmp_path, monkeypatch):
    """A trust store stat'd as owned by a different UID is refused.

    If the UID guard (``st.st_uid == os.getuid()``) were removed,
    this test would fail.

    Skips cleanly on Windows (no UID concept).
    """
    if not hasattr(os, "getuid"):
        pytest.skip("UID ownership check applies only on POSIX systems")
    monkeypatch.setenv("BOUNDED_LOOPS_TRUST_STORE", str(tmp_path / "trust.json"))
    record_trust(tmp_path, "pytest -q")
    # Monkey-patch os.getuid so the ownership comparison sees a DIFFERENT uid
    real_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)
    assert is_trusted(tmp_path, "pytest -q") is False, (
        "trust store owned by a foreign UID must be refused"
    )


# ── CON-03: atomic write + symlink guard in _save() ──────────────────────────


def test_save_writes_file_with_0600_mode(tmp_path, monkeypatch):
    """record_trust() must create the store file with mode 0o600."""
    store_path = tmp_path / "trust.json"
    monkeypatch.setenv("BOUNDED_LOOPS_TRUST_STORE", str(store_path))
    record_trust(tmp_path, "cmd")
    mode = store_path.stat().st_mode & 0o777
    assert mode == 0o600, f"store file must be 0600, got 0o{mode:03o}"


def test_save_refuses_symlink_at_store_path(tmp_path, monkeypatch):
    """record_trust() must refuse to write through a symlink at the store path.

    Symlink-following would let an attacker redirect the trust store write
    to an attacker-controlled path (TOCTOU).
    """
    store_path = tmp_path / "trust.json"
    target = tmp_path / "attacker_controlled.json"
    target.write_text("{}", encoding="utf-8")
    store_path.symlink_to(target)
    monkeypatch.setenv("BOUNDED_LOOPS_TRUST_STORE", str(store_path))
    with pytest.raises(OSError) as exc_info:
        record_trust(tmp_path, "cmd")
    assert exc_info.value.errno in (errno.ELOOP, errno.EEXIST, errno.EMLINK), (
        f"expected ELOOP/EEXIST/EMLINK, got {exc_info.value.errno}"
    )
    # The symlink target must not have been modified
    assert json.loads(target.read_text()) == {}, "attacker-controlled file must not be written"


def test_save_does_not_corrupt_store_on_json_error(tmp_path, monkeypatch):
    """A failure before writing must leave the existing store intact (atomicity).

    Mutation proof for the CON-03 fix: with the OLD O_TRUNC-then-write
    implementation, os.open(..., O_TRUNC) empties the file BEFORE the
    write begins, so a failure between open and write leaves zero bytes.
    The new temp-file-then-replace approach serialises JSON BEFORE
    touching the destination, so a serialisation error leaves the
    original untouched.
    """
    import bounded_loops.trust_store as _ts

    store_path = tmp_path / "trust.json"
    monkeypatch.setenv("BOUNDED_LOOPS_TRUST_STORE", str(store_path))

    # Write an initial valid record
    record_trust(tmp_path, "original-cmd")
    original_bytes = store_path.read_bytes()
    assert original_bytes  # sanity: non-empty

    # Patch json.dumps to fail on the second call (the second record_trust).
    # With OLD code: os.open(O_TRUNC) empties the file before json.dumps is
    # reached, so the file ends up zero bytes → assertion below fails (RED).
    # With NEW code: json.dumps is called BEFORE creating any temp file, so a
    # failure here leaves the destination untouched → assertion passes (GREEN).
    # The monkeypatch is applied AFTER the initial record_trust, so the first
    # call through the patched path is call_count=1.  Raise immediately (>= 1)
    # to simulate a serialisation failure before any file is created/modified.
    call_count = [0]
    real_dumps = json.dumps

    def _failing_dumps(obj, **kw):
        call_count[0] += 1
        if call_count[0] >= 1:
            raise ValueError("simulated json serialisation failure")
        return real_dumps(obj, **kw)

    monkeypatch.setattr(_ts.json, "dumps", _failing_dumps)

    with pytest.raises(ValueError):
        record_trust(tmp_path, "new-cmd")

    assert store_path.read_bytes() == original_bytes, (
        "a write error must not corrupt the existing trust store"
    )


def test_save_leaves_no_temp_files_on_success(tmp_path, monkeypatch):
    """No temporary files must remain in the store directory after a successful write."""
    store_path = tmp_path / "trust.json"
    monkeypatch.setenv("BOUNDED_LOOPS_TRUST_STORE", str(store_path))
    record_trust(tmp_path, "cmd")
    # Existence obligation (0.6.5): an empty directory would satisfy "no temp files remain"
    # while proving the store was never written at all.
    assert store_path.exists(), "the store was not written; a leftover check over nothing passes"
    leftover = [
        p for p in store_path.parent.iterdir()
        if p != store_path and p.name.startswith(".trust_tmp_")
    ]
    assert not leftover, f"temp files left behind: {leftover}"


def test_save_leaves_no_temp_files_on_failure(tmp_path, monkeypatch):
    """Temp files must be cleaned up even when the write fails."""
    import bounded_loops.trust_store as _ts

    store_path = tmp_path / "trust.json"
    monkeypatch.setenv("BOUNDED_LOOPS_TRUST_STORE", str(store_path))

    # Make the directory so the temp-file check is meaningful
    store_path.parent.mkdir(parents=True, exist_ok=True)

    call_count = [0]
    real_dumps = json.dumps

    def _failing_dumps(obj, **kw):
        call_count[0] += 1
        if call_count[0] >= 1:
            raise ValueError("forced failure")
        return real_dumps(obj, **kw)

    monkeypatch.setattr(_ts.json, "dumps", _failing_dumps)

    with pytest.raises(ValueError):
        record_trust(tmp_path, "cmd")

    leftover = list(store_path.parent.glob(".trust_tmp_*"))
    assert not leftover, f"temp file not cleaned up after failure: {leftover}"
