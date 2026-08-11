"""RED-first tests for `bl graph init`'s config I/O layer (Slice 4).

Covers path resolution precedence, allowlist-entry canonicalization/dedup, the
secure (0600/0700, O_NOFOLLOW) write, existing-config detection, and — the
non-negotiable — the round trip through `egress_posture.resolve_egress_posture`,
the SAME fail-closed reader `bl graph run` consumes. `tests/conftest.py`'s
autouse `_isolate_trust_store` fixture already redirects HOME to a per-test
tmp dir; every test here additionally passes an explicit path so it is never
ambiguous which file is under test.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from bounded_loops.graph.adapters.enforcement import egress_posture
from bounded_loops.graph.adapters.enforcement.egress_posture import EgressPosture, EgressPostureConfig
from bounded_loops.graph.domain.errors import GraphValidationError
from bounded_loops.graph.init import config_writer
from bounded_loops.graph.init.errors import GraphInitError

# ── default_config_path / resolve_config_path ───────────────────────────────────


def test_default_config_path_is_home_dot_bounded_loops_egress_json() -> None:
    # HOME is already redirected to a per-test tmp dir by the autouse fixture in
    # tests/conftest.py, so this never touches the real machine's home directory.
    assert config_writer.default_config_path() == Path.home() / ".bounded-loops" / "egress.json"


def test_resolve_config_path_prefers_cli_arg_over_env_and_default(tmp_path: Path) -> None:
    cli_path = tmp_path / "from-cli.json"
    resolved = config_writer.resolve_config_path(
        str(cli_path), {"BOUNDED_LOOPS_EGRESS_CONFIG": str(tmp_path / "from-env.json")},
    )
    assert resolved == cli_path


def test_resolve_config_path_falls_back_to_env_var_when_no_cli_arg(tmp_path: Path) -> None:
    env_path = tmp_path / "from-env.json"
    resolved = config_writer.resolve_config_path(None, {"BOUNDED_LOOPS_EGRESS_CONFIG": str(env_path)})
    assert resolved == env_path


def test_resolve_config_path_falls_back_to_default_when_nothing_given() -> None:
    resolved = config_writer.resolve_config_path(None, {})
    assert resolved == config_writer.default_config_path()


def test_resolve_config_path_treats_empty_cli_arg_as_absent(tmp_path: Path) -> None:
    env_path = tmp_path / "from-env.json"
    resolved = config_writer.resolve_config_path("", {"BOUNDED_LOOPS_EGRESS_CONFIG": str(env_path)})
    assert resolved == env_path


# ── allowlist flag flattening ────────────────────────────────────────────────────


def test_flatten_allowlist_flag_splits_commas_and_strips_blanks() -> None:
    assert config_writer.flatten_allowlist_flag(["api.anthropic.com, , api.openai.com,"]) == (
        "api.anthropic.com",
        "api.openai.com",
    )


def test_flatten_allowlist_flag_supports_repeated_flag_values() -> None:
    assert config_writer.flatten_allowlist_flag(["api.anthropic.com", "internal.example.com:8443"]) == (
        "api.anthropic.com",
        "internal.example.com:8443",
    )


def test_flatten_allowlist_flag_of_empty_sequence_is_empty() -> None:
    assert config_writer.flatten_allowlist_flag([]) == ()


# ── allowlist entry canonicalization (reuses the reader's own public parsers) ──


def test_canonicalize_allowlist_entry_accepts_bare_host() -> None:
    assert config_writer.canonicalize_allowlist_entry("api.anthropic.com") == "api.anthropic.com"


def test_canonicalize_allowlist_entry_lowercases_and_keeps_explicit_non_default_port() -> None:
    assert config_writer.canonicalize_allowlist_entry("Internal.Example.COM:8443") == "internal.example.com:8443"


def test_canonicalize_allowlist_entry_drops_explicit_default_port() -> None:
    # host:443 and host normalize to the SAME NetworkDestination; writing the
    # bare form keeps the file minimal and matches what a bare host already means.
    assert config_writer.canonicalize_allowlist_entry("api.anthropic.com:443") == "api.anthropic.com"


def test_canonicalize_allowlist_entry_rejects_ip_literal() -> None:
    with pytest.raises(GraphInitError, match="invalid allowlist entry"):
        config_writer.canonicalize_allowlist_entry("203.0.113.5")


def test_canonicalize_allowlist_entry_rejects_malformed_port() -> None:
    with pytest.raises(GraphInitError, match="invalid allowlist entry"):
        config_writer.canonicalize_allowlist_entry("api.example.com:not-a-port")


def test_canonicalize_allowlist_entry_rejects_empty_string() -> None:
    with pytest.raises(GraphInitError):
        config_writer.canonicalize_allowlist_entry("")


def test_canonicalize_allowlist_entries_dedupes_case_and_default_port_variants() -> None:
    result = config_writer.canonicalize_allowlist_entries(["API.Anthropic.COM", "api.anthropic.com:443"])
    assert result == ("api.anthropic.com",)


def test_canonicalize_allowlist_entries_preserves_first_seen_order() -> None:
    result = config_writer.canonicalize_allowlist_entries(["b.example.com", "a.example.com"])
    assert result == ("b.example.com", "a.example.com")


def test_canonicalize_allowlist_entries_raises_on_first_bad_entry() -> None:
    with pytest.raises(GraphInitError, match="not-a-real-host!!"):
        config_writer.canonicalize_allowlist_entries(["api.anthropic.com", "not-a-real-host!!"])


def test_default_allowlist_port_constant_matches_egress_posture_modules_own_default() -> None:
    # A plain int constant (no Path/mutable-global staleness hazard, unlike
    # _DEFAULT_CONFIG_PATH) — safe to pin directly against the reader's own value
    # so the two modules can never silently drift apart on what "bare host" means.
    assert config_writer._DEFAULT_ALLOWLIST_PORT == egress_posture._DEFAULT_ALLOWLIST_PORT


# ── build_config_payload ─────────────────────────────────────────────────────────


def test_build_config_payload_open_omits_allowlist_key() -> None:
    assert config_writer.build_config_payload(EgressPosture.OPEN, ()) == {"posture": "open"}


def test_build_config_payload_broker_omits_allowlist_key() -> None:
    assert config_writer.build_config_payload(EgressPosture.BROKER, ()) == {"posture": "broker"}


def test_build_config_payload_allowlist_includes_hosts_as_a_list() -> None:
    payload = config_writer.build_config_payload(EgressPosture.ALLOWLIST, ("api.anthropic.com",))
    assert payload == {"posture": "allowlist", "allowlist": ["api.anthropic.com"]}


def test_build_config_payload_dedupes_and_canonicalizes_allowlist_defensively() -> None:
    # m3: the package API alone (not just the cli_init caller) must never be able to
    # emit a uniqueness-violating allowlist the reader would reject on read-back.
    payload = config_writer.build_config_payload(
        EgressPosture.ALLOWLIST, ["API.Anthropic.COM", "api.anthropic.com:443"],
    )
    assert payload == {"posture": "allowlist", "allowlist": ["api.anthropic.com"]}


def test_build_config_payload_raises_on_an_invalid_allowlist_entry() -> None:
    with pytest.raises(GraphInitError, match="invalid allowlist entry"):
        config_writer.build_config_payload(EgressPosture.ALLOWLIST, ["203.0.113.5"])


# ── write_config_atomically ───────────────────────────────────────────────────────


def test_write_config_atomically_creates_parent_dir_mode_0700(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "egress.json"
    config_writer.write_config_atomically(target, {"posture": "open"})
    mode = stat.S_IMODE(target.parent.stat().st_mode)
    assert mode == 0o700


def test_write_config_atomically_writes_file_mode_0600(tmp_path: Path) -> None:
    target = tmp_path / "egress.json"
    config_writer.write_config_atomically(target, {"posture": "open"})
    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o600


def test_write_config_atomically_writes_valid_json(tmp_path: Path) -> None:
    target = tmp_path / "egress.json"
    config_writer.write_config_atomically(target, {"posture": "allowlist", "allowlist": ["api.anthropic.com"]})
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "posture": "allowlist", "allowlist": ["api.anthropic.com"],
    }


def test_write_config_atomically_overwrites_an_existing_regular_file(tmp_path: Path) -> None:
    target = tmp_path / "egress.json"
    target.write_text(json.dumps({"posture": "broker"}), encoding="utf-8")
    config_writer.write_config_atomically(target, {"posture": "open"})
    assert json.loads(target.read_text(encoding="utf-8")) == {"posture": "open"}


def test_write_config_atomically_refuses_a_symlinked_target(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    real.write_text(json.dumps({"posture": "open"}), encoding="utf-8")
    link = tmp_path / "egress.json"
    link.symlink_to(real)
    with pytest.raises(GraphInitError, match="symlink"):
        config_writer.write_config_atomically(link, {"posture": "broker"})
    # the symlink's target must be untouched by the refused write
    assert json.loads(real.read_text(encoding="utf-8")) == {"posture": "open"}


def test_write_config_atomically_refuses_a_dangling_symlinked_target(tmp_path: Path) -> None:
    link = tmp_path / "egress.json"
    link.symlink_to(tmp_path / "does-not-exist.json")
    with pytest.raises(GraphInitError, match="symlink"):
        config_writer.write_config_atomically(link, {"posture": "open"})


def test_write_config_atomically_return_value_is_the_verified_egress_posture_config(tmp_path: Path) -> None:
    target = tmp_path / "egress.json"
    verified = config_writer.write_config_atomically(target, {"posture": "broker"})
    assert isinstance(verified, EgressPostureConfig)
    assert verified.posture is EgressPosture.BROKER


# ── M1 (Grok, live-proven): mode 0600 must be FORCED on OVERWRITE too ──────────
#
# POSIX only applies the `mode` argument to os.open() at file CREATION. An
# in-place O_CREAT|O_TRUNC open of an EXISTING inode keeps that inode's OLD
# mode — truncating the CONTENT but silently leaving a looser mode (e.g. a
# pre-existing 0o666) in place. Reproduced live before this fix: pre-create at
# 0o666, overwrite via the installer, mode stayed 0o666 while content updated.
# The fix writes a FRESH inode (a temp file, explicitly fchmod'd to 0600) and
# os.replace()s it into place — the target's final mode is always the fresh
# inode's mode, never the old inode's, regardless of what was there before.


def test_write_config_atomically_forces_0600_even_when_overwriting_a_looser_existing_mode(
    tmp_path: Path,
) -> None:
    target = tmp_path / "egress.json"
    target.write_text(json.dumps({"posture": "broker"}), encoding="utf-8")
    target.chmod(0o666)
    assert stat.S_IMODE(target.stat().st_mode) == 0o666  # sanity: the setup actually took

    config_writer.write_config_atomically(target, {"posture": "open"})

    assert json.loads(target.read_text(encoding="utf-8")) == {"posture": "open"}
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_write_config_atomically_forces_0600_even_when_overwriting_a_tighter_existing_mode(
    tmp_path: Path,
) -> None:
    # The other direction too: a previously-0400 (read-only) file must still end
    # up exactly 0600 after overwrite, not "whatever was already narrower".
    target = tmp_path / "egress.json"
    target.write_text(json.dumps({"posture": "broker"}), encoding="utf-8")
    target.chmod(0o400)

    config_writer.write_config_atomically(target, {"posture": "allowlist", "allowlist": []})

    assert stat.S_IMODE(target.stat().st_mode) == 0o600


# ── m1 + rollback: the temp is verified BEFORE it ever replaces the target ─────


def test_write_config_atomically_rolls_back_on_a_verify_failure_leaving_the_old_config_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Its own subdirectory: tmp_path itself also holds the autouse HOME-redirect
    # fixture's "home" dir (tests/conftest.py), which would otherwise pollute a
    # "no litter" listing unrelated to this test's own target directory.
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    target = work_dir / "egress.json"
    good_content = json.dumps({"posture": "broker"})
    target.write_text(good_content, encoding="utf-8")
    original_mode = stat.S_IMODE(target.stat().st_mode)

    def _boom(_path: Path) -> EgressPostureConfig:
        raise GraphValidationError("egress_posture", "/x", "forced failure for the test")

    monkeypatch.setattr(config_writer, "verify_round_trip", _boom)
    with pytest.raises(GraphInitError, match="internal error"):
        config_writer.write_config_atomically(target, {"posture": "open"})

    # the PREVIOUS good config must be byte-for-byte and mode-for-mode intact.
    assert target.read_text(encoding="utf-8") == good_content
    assert stat.S_IMODE(target.stat().st_mode) == original_mode
    # ...and no temp litter left behind either.
    assert {p.name for p in work_dir.iterdir()} == {"egress.json"}


def test_write_config_atomically_rolls_back_when_the_target_did_not_exist_yet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "nested" / "egress.json"

    def _boom(_path: Path) -> EgressPostureConfig:
        raise GraphValidationError("egress_posture", "/x", "forced failure for the test")

    monkeypatch.setattr(config_writer, "verify_round_trip", _boom)
    with pytest.raises(GraphInitError, match="internal error"):
        config_writer.write_config_atomically(target, {"posture": "open"})

    assert not target.exists()
    assert list(target.parent.iterdir()) == []  # no temp litter in the (created) parent dir


# ── no temp-file litter, on success OR failure ──────────────────────────────────


def test_write_config_atomically_leaves_no_temp_file_after_success(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "egress.json"
    config_writer.write_config_atomically(target, {"posture": "open"})
    # the temp file was created in the SAME directory as the target (os.replace
    # requires the same filesystem) and must not survive a successful replace.
    assert {p.name for p in target.parent.iterdir()} == {"egress.json"}


def test_write_config_atomically_leaves_no_temp_file_after_a_write_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Own subdirectory: see the rollback test above for why (tmp_path itself
    # also holds the autouse HOME-redirect fixture's "home" dir).
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    target = work_dir / "egress.json"

    def _boom(_fd: int) -> None:
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(config_writer.os, "fsync", _boom)  # monkeypatch auto-restores at teardown
    with pytest.raises(GraphInitError, match="could not write"):
        config_writer.write_config_atomically(target, {"posture": "open"})

    assert not target.exists()
    assert list(work_dir.iterdir()) == []


# ── symlink safety holds even if the early check is bypassed/raced (TOCTOU) ────


def test_write_config_atomically_never_writes_through_a_symlink_even_if_the_precheck_is_bypassed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Simulates the TOCTOU window: the early islink() check is fooled/raced (made
    # to report False) even though `target` really is a symlink at call time. The
    # guarantee against writing THROUGH a symlink must hold unconditionally because
    # of os.replace()'s own kernel-level "atomically repoint the directory entry,
    # never open/follow the old target" semantics — not because of the early
    # check, which is a fast, friendly-message nicety for the common case only.
    important = tmp_path / "important.json"
    important.write_text("do-not-touch", encoding="utf-8")
    target = tmp_path / "egress.json"
    target.symlink_to(important)

    monkeypatch.setattr(config_writer.os.path, "islink", lambda _p: False)
    config_writer.write_config_atomically(target, {"posture": "open"})

    # important.json (the symlink's former target) must be COMPLETELY untouched...
    assert important.read_text(encoding="utf-8") == "do-not-touch"
    # ...while `target` now names a fresh, real, correct config file — the
    # symlink ENTRY was atomically replaced, never written through.
    assert not target.is_symlink()
    assert json.loads(target.read_text(encoding="utf-8")) == {"posture": "open"}


# ── verify_round_trip — the non-negotiable proof ────────────────────────────────


def test_verify_round_trip_reads_back_open_posture(tmp_path: Path) -> None:
    target = tmp_path / "egress.json"
    config_writer.write_config_atomically(target, config_writer.build_config_payload(EgressPosture.OPEN, ()))
    resolved = config_writer.verify_round_trip(target)
    assert resolved.posture is EgressPosture.OPEN
    assert resolved.allowlist == ()


def test_verify_round_trip_reads_back_allowlist_posture_and_hosts(tmp_path: Path) -> None:
    target = tmp_path / "egress.json"
    hosts = config_writer.canonicalize_allowlist_entries(["api.anthropic.com", "internal.example.com:8443"])
    config_writer.write_config_atomically(target, config_writer.build_config_payload(EgressPosture.ALLOWLIST, hosts))
    resolved = config_writer.verify_round_trip(target)
    assert resolved.posture is EgressPosture.ALLOWLIST
    assert resolved.allowlist_admits("api.anthropic.com", 443) is True
    assert resolved.allowlist_admits("internal.example.com", 8443) is True
    assert resolved.allowlist_admits("evil.example.com", 443) is False


def test_verify_round_trip_reads_back_broker_posture(tmp_path: Path) -> None:
    target = tmp_path / "egress.json"
    config_writer.write_config_atomically(target, config_writer.build_config_payload(EgressPosture.BROKER, ()))
    resolved = config_writer.verify_round_trip(target)
    assert resolved.posture is EgressPosture.BROKER


def test_verify_round_trip_is_isolated_from_ambient_env_vars(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # CRIT-derived: a real shell might have BOUNDED_LOOPS_EGRESS_POSTURE exported for an
    # unrelated purpose (env beats file in real resolution). verify_round_trip must prove
    # what the FILE says, not what the ambient environment would shadow it to — otherwise
    # it could report a false "verified open" for a file that actually says "allowlist".
    monkeypatch.setenv("BOUNDED_LOOPS_EGRESS_POSTURE", "open")
    monkeypatch.setenv("BOUNDED_LOOPS_EGRESS_ALLOWLIST", "should-never-be-read.example.com")
    target = tmp_path / "egress.json"
    hosts = config_writer.canonicalize_allowlist_entries(["api.anthropic.com"])
    config_writer.write_config_atomically(target, config_writer.build_config_payload(EgressPosture.ALLOWLIST, hosts))
    resolved = config_writer.verify_round_trip(target)
    assert resolved.posture is EgressPosture.ALLOWLIST
    assert resolved.allowlist_admits("api.anthropic.com", 443) is True
    assert resolved.allowlist_admits("should-never-be-read.example.com", 443) is False


def test_verify_round_trip_raises_on_a_hand_corrupted_file(tmp_path: Path) -> None:
    target = tmp_path / "egress.json"
    target.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(Exception, match="not valid JSON"):
        config_writer.verify_round_trip(target)


# ── read_existing_snapshot ───────────────────────────────────────────────────────


def test_read_existing_snapshot_returns_none_when_absent(tmp_path: Path) -> None:
    assert config_writer.read_existing_snapshot(tmp_path / "does-not-exist.json") is None


def test_read_existing_snapshot_detects_a_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    real.write_text(json.dumps({"posture": "open"}), encoding="utf-8")
    link = tmp_path / "egress.json"
    link.symlink_to(real)
    snapshot = config_writer.read_existing_snapshot(link)
    assert snapshot is not None
    assert snapshot.is_symlink is True
    assert snapshot.config is None


def test_read_existing_snapshot_reads_a_valid_existing_config(tmp_path: Path) -> None:
    target = tmp_path / "egress.json"
    target.write_text(json.dumps({"posture": "broker"}), encoding="utf-8")
    snapshot = config_writer.read_existing_snapshot(target)
    assert snapshot is not None
    assert snapshot.is_symlink is False
    assert snapshot.error is None
    assert snapshot.config is not None
    assert snapshot.config.posture is EgressPosture.BROKER


def test_read_existing_snapshot_reports_a_corrupt_existing_config(tmp_path: Path) -> None:
    target = tmp_path / "egress.json"
    target.write_text("{not valid json", encoding="utf-8")
    snapshot = config_writer.read_existing_snapshot(target)
    assert snapshot is not None
    assert snapshot.config is None
    assert snapshot.error is not None
    assert "not valid JSON" in snapshot.error
